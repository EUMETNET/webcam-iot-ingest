from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from database.registry_queries import DueSourceStream
from ingestion.skaping.skaping_image_access import SkapingImageReference
from ingestion.windy.windy_ingestion_workflow import _process_job


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _job(*, timestamp: datetime | None, marker: str | None) -> DueSourceStream:
    return DueSourceStream(
        source_stream_id="ska272view1",
        site_id="ska272",
        network_id="ska",
        provider_site_id="272",
        provider_source_stream_id="view1",
        selected_rendition="mini",
        site_metadata={},
        source_stream_metadata={"name": "Mountain view"},
        latitude=45.0,
        longitude=6.0,
        altitude=1200,
        country="FR",
        corrected_latitude=None,
        corrected_longitude=None,
        corrected_altitude=None,
        last_download_timestamp=None,
        last_observed_provider_timestamp=timestamp,
        last_observed_image_marker=marker,
        last_processed_timestamp=timestamp,
        estimated_source_stream_period=300,
    )


def _client(*, marker: str, timestamp: datetime | None) -> Mock:
    client = Mock()
    client.get_current_image.return_value = SkapingImageReference(
        "view1",
        marker,
        "https://objects.example/mini.jpg",
        timestamp,
        "/mini.jpg",
    )
    client.download.return_value = PNG_1PX
    client.downloaded_marker.return_value = None
    return client


def test_unchanged_etag_stops_even_when_last_modified_changes() -> None:
    previous = datetime(2026, 8, 19, 12, tzinfo=UTC)
    client = _client(
        marker='"etag-v1"', timestamp=previous + timedelta(minutes=5)
    )

    result = _process_job(
        client,
        _job(timestamp=previous, marker='"etag-v1"'),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "unchanged"
    assert result.state_update is None
    client.download.assert_not_called()


def test_changed_etag_downloads_without_last_modified() -> None:
    client = _client(marker='"etag-v2"', timestamp=None)

    result = _process_job(
        client,
        _job(timestamp=None, marker='"etag-v1"'),
        dry_run=True,
        minimum_period_seconds=300,
    )

    assert result.outcome == "downloaded"
    client.download.assert_called_once()
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp is None
    assert result.state_update.provider_image_marker == '"etag-v2"'
    assert result.period_estimate_candidate is None
