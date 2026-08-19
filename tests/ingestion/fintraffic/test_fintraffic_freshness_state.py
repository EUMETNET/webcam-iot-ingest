from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from database.registry_queries import DueSourceStream
from ingestion.fintraffic.fintraffic_image_access import (
    FintrafficImageAccessError,
    FintrafficImageReference,
)
from ingestion.windy.windy_ingestion_workflow import _process_job
from storage.s3_storage import StoredObject


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _job(*, timestamp: datetime | None, marker: str | None) -> DueSourceStream:
    return DueSourceStream(
        source_stream_id="finC0150301",
        site_id="finC01503",
        network_id="fin",
        provider_site_id="C01503",
        provider_source_stream_id="C0150301",
        selected_rendition="full_jpeg",
        site_metadata={},
        source_stream_metadata={"presentationName": "Road view"},
        latitude=60.0,
        longitude=24.0,
        altitude=None,
        country="FI",
        corrected_latitude=None,
        corrected_longitude=None,
        corrected_altitude=None,
        last_download_timestamp=None,
        last_observed_provider_timestamp=timestamp,
        last_observed_image_marker=marker,
        last_processed_timestamp=timestamp,
        estimated_source_stream_period=600,
    )


def _client(timestamp: datetime, etag: str) -> Mock:
    client = Mock()
    client.get_current_image.return_value = FintrafficImageReference(
        "C0150301", "https://images.example/C0150301.jpg", timestamp
    )
    client.download.return_value = PNG_1PX
    client.downloaded_marker.return_value = etag
    return client


def test_equal_bulk_timestamp_stops_before_jpeg_download() -> None:
    timestamp = datetime(2026, 8, 19, 12, tzinfo=UTC)
    client = _client(timestamp, '"etag-v1"')

    result = _process_job(
        client,
        _job(timestamp=timestamp, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "unchanged"
    assert result.state_update is None
    client.download.assert_not_called()


def test_new_bulk_timestamp_and_same_etag_retains_download_decision_state() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    current = previous + timedelta(minutes=10)
    client = _client(current, '"etag-v1"')

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "unchanged"
    assert result.provider_marker == '"etag-v1"'
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == current
    assert result.state_update.provider_image_marker == '"etag-v1"'
    assert result.state_update.download_timestamp is not None
    assert result.state_update.processed_timestamp is None
    assert result.period_estimate_candidate is not None


def test_failure_before_etag_observation_retains_timestamp_only() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    current = previous + timedelta(minutes=10)
    client = _client(current, '"etag-v2"')
    client.download.side_effect = FintrafficImageAccessError("temporary failure")
    client.downloaded_marker.return_value = None

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "download_error"
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == current
    assert result.state_update.provider_image_marker is None
    assert result.state_update.download_timestamp is not None
    assert result.state_update.processed_timestamp is None


def test_body_failure_retains_already_validated_etag_observation() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    current = previous + timedelta(minutes=10)
    client = _client(current, '"etag-v2"')
    client.download.side_effect = FintrafficImageAccessError("invalid body")
    client.downloaded_marker.return_value = '"etag-v2"'

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "download_error"
    assert result.provider_marker == '"etag-v2"'
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == current
    assert result.state_update.provider_image_marker == '"etag-v2"'
    assert result.state_update.download_timestamp is not None
    assert result.state_update.processed_timestamp is None


def test_new_bulk_timestamp_and_new_etag_continues_image_processing() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    current = previous + timedelta(minutes=10)
    client = _client(current, '"etag-v2"')

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=True,
        minimum_period_seconds=300,
    )

    assert result.outcome == "downloaded"
    assert result.provider_marker == '"etag-v2"'
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == current
    assert result.state_update.provider_image_marker == '"etag-v2"'
    assert (result.width, result.height, result.image_format) == (1, 1, "PNG")


def test_measured_time_reaches_notification_and_processed_state_after_publish() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    current = previous + timedelta(minutes=10)
    client = _client(current, '"etag-v2"')
    storage = Mock(prefix="")
    stored = StoredObject(
        "bucket", "T0V0/fin/image.jpg", "https://objects.example/image.jpg"
    )
    storage.reference.return_value = stored
    storage.upload.return_value = stored
    publisher = Mock()
    publisher.publish.return_value = "webcam/T0V0"

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
        storage=storage,
        publisher=publisher,
    )

    notification = publisher.publish.call_args.args[1]
    assert notification["timestamps"]["provider_update_timestamp"] == (
        "2026-08-19T12:10:00Z"
    )
    assert result.outcome == "published"
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == current
    assert result.state_update.provider_image_marker == '"etag-v2"'
    assert result.state_update.processed_timestamp is not None
