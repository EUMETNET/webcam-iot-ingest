from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import Mock

from database.registry_queries import DueSourceStream, build_period_estimate_candidate
from ingestion.fintraffic.fintraffic_image_access import FintrafficImageReference
from ingestion.windy.windy_image_access import WindyImageReference
from ingestion.shared.source_processing import (
    _available_freshness_unchanged,
    _estimated_period_band,
    process_job,
)
from ingestion.windy.windy_ingestion_workflow import (
    _select_country_sample,
    _validate_countries,
)
from ingestion.notification.mqtt_publisher import MqttPublicationError
from ingestion.windy.windy_image_access import WindyImageAccessError
from storage.s3_storage import StoredObject


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def job(
    *,
    marker: str | None = None,
    provider_timestamp=None,
) -> DueSourceStream:
    return DueSourceStream(
        source_stream_id="win42",
        site_id="win42",
        network_id="win",
        provider_site_id="42",
        provider_source_stream_id="42",
        selected_rendition="preview",
        site_metadata={},
        source_stream_metadata={"title": "Harbour view"},
        latitude=55.0,
        longitude=12.0,
        altitude=5.0,
        country="DK",
        corrected_latitude=None,
        corrected_longitude=None,
        corrected_altitude=None,
        last_download_timestamp=None,
        last_observed_provider_timestamp=provider_timestamp,
        last_observed_image_marker=marker,
        last_processed_timestamp=provider_timestamp,
        estimated_source_stream_period=None,
    )


def test_unchanged_provider_timestamp_skips_download() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2026-07-30T12:00:00Z", "https://images.example/a.jpg"
    )

    result = process_job(
        client,
        job(provider_timestamp=datetime(2026, 7, 30, 12, tzinfo=timezone.utc)),
        dry_run=True,
        minimum_period_seconds=300,
    )

    assert result.outcome == "unchanged"
    client.download.assert_not_called()


def test_freshness_comparison_uses_every_available_indicator() -> None:
    timestamp = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

    assert _available_freshness_unchanged(
        job(marker="etag"), marker="etag", provider_timestamp=None
    )
    assert _available_freshness_unchanged(
        job(provider_timestamp=timestamp),
        marker=None,
        provider_timestamp=timestamp,
    )
    assert _available_freshness_unchanged(
        job(marker="etag", provider_timestamp=timestamp),
        marker="etag",
        provider_timestamp=timestamp,
    )
    assert not _available_freshness_unchanged(
        job(marker="etag", provider_timestamp=timestamp),
        marker="etag",
        provider_timestamp=timestamp + timedelta(minutes=5),
    )


def test_unchanged_freshness_does_not_create_state_update() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2026-07-30T12:00:00Z", "https://images.example/a.jpg"
    )
    result = process_job(
        client,
        job(provider_timestamp=datetime(2026, 7, 30, 12, tzinfo=timezone.utc)),
        dry_run=False,
        minimum_period_seconds=300,
    )

    assert result.outcome == "unchanged"
    assert result.state_update is None


def test_dry_run_decodes_image_without_database_update() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "new", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX
    result = process_job(client, job(), dry_run=True, minimum_period_seconds=300)

    assert result.outcome == "downloaded"
    assert (result.width, result.height, result.image_format) == (1, 1, "PNG")
    assert result.state_update is not None


def test_first_provider_observation_does_not_invent_a_period() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2026-07-30T12:00:00Z", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX

    result = process_job(
        client,
        job(),
        dry_run=True,
        minimum_period_seconds=300,
    )

    assert result.period_estimate_candidate is None


def test_bounded_minimum_period_starts_null_and_uses_provider_gaps() -> None:
    previous = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    first = build_period_estimate_candidate(
        job(),
        provider_update_timestamp=previous,
        minimum_period_seconds=300,
    )
    assert first is None

    without_estimate = job(provider_timestamp=previous)
    learned = build_period_estimate_candidate(
        without_estimate,
        provider_update_timestamp=previous + timedelta(seconds=620),
        minimum_period_seconds=300,
    )
    assert learned is not None and learned.estimated_source_stream_period == 620

    existing = replace(without_estimate, estimated_source_stream_period=600)
    long_gap = build_period_estimate_candidate(
        existing,
        provider_update_timestamp=previous + timedelta(seconds=1200),
        minimum_period_seconds=300,
    )
    short_gap = build_period_estimate_candidate(
        existing,
        provider_update_timestamp=previous + timedelta(seconds=100),
        minimum_period_seconds=300,
    )
    assert long_gap is not None and long_gap.estimated_source_stream_period == 600
    assert short_gap is not None and short_gap.estimated_source_stream_period == 300


def test_estimated_period_band_preserves_unknown_initial_state() -> None:
    assert _estimated_period_band(None) == "unknown"
    assert _estimated_period_band(600) == "le_10m"
    assert _estimated_period_band(601) == "10m_to_60m"
    assert _estimated_period_band(3601) == "gt_60m"


def test_published_job_emits_latency_payload_derived_and_duration() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2020-01-01T00:00:00Z", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX
    storage = Mock(prefix="")
    storage.reference.return_value = StoredObject(
        "bucket", "T0V0/win/image.jpg", "https://objects.example/image.jpg"
    )
    storage.upload.return_value = storage.reference.return_value
    publisher = Mock()
    publisher.publish.return_value = "webcam/T0V0"
    events = []
    result = process_job(
        client,
        job(),
        dry_run=False,
        minimum_period_seconds=300,
        storage=storage,
        publisher=publisher,
        event_observer=lambda event, values: events.append((event, values)),
    )

    assert result.outcome == "published"
    assert result.state_update is not None
    assert result.state_update.processed_timestamp is not None
    assert result.state_update.processed_timestamp > datetime(
        2020, 1, 1, tzinfo=timezone.utc
    )
    measures = {
        values["measure"]
        for event, values in events
        if event == "image_latency"
    }
    assert measures == {
        "provider_to_download",
        "download_to_job_end",
        "provider_to_job_end",
    }
    assert {
        values["estimated_period_band"]
        for event, values in events
        if event == "image_latency"
    } == {"unknown"}
    assert sum(event == "job_completed" for event, _ in events) == 1
    assert sum(event == "derived_image" for event, _ in events) == 1


def test_download_failure_leaves_processed_state_for_next_attempt() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2026-07-30T12:00:00Z", "https://images.example/a.png"
    )
    client.download.side_effect = [WindyImageAccessError("temporary"), PNG_1PX]
    first = process_job(client, job(), dry_run=False, minimum_period_seconds=300)
    second = process_job(client, job(), dry_run=False, minimum_period_seconds=300)

    assert first.outcome == "download_error"
    assert second.outcome == "downloaded"
    assert first.state_update is not None
    assert first.state_update.processed_timestamp is None
    assert second.state_update is not None
    assert second.state_update.processed_timestamp is None


def test_publication_failure_leaves_processed_state_for_next_attempt() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2026-07-30T12:00:00Z", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX
    storage = Mock(prefix="")
    stored = StoredObject(
        "bucket", "T0V0/win/image.jpg", "https://objects.example/image.jpg"
    )
    storage.reference.return_value = stored
    storage.upload.return_value = stored
    publisher = Mock()
    publisher.publish.side_effect = [
        MqttPublicationError("ambiguous failure"),
        "webcam/T0V0",
    ]
    first = process_job(
        client,
        job(),
        dry_run=False,
        minimum_period_seconds=300,
        storage=storage,
        publisher=publisher,
    )
    second = process_job(
        client,
        job(),
        dry_run=False,
        minimum_period_seconds=300,
        storage=storage,
        publisher=publisher,
    )

    assert first.outcome == "mqtt_error"
    assert second.outcome == "published"
    assert storage.upload.call_count == 2
    assert first.state_update is not None
    assert first.state_update.processed_timestamp is None
    assert second.state_update is not None
    assert second.state_update.processed_timestamp is not None


def test_fintraffic_unchanged_etag_discards_image_but_returns_decision_state() -> None:
    timestamp = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
    fintraffic_job = replace(
        job(marker='"same-etag"'),
        network_id="fin",
        selected_rendition="full_jpeg",
        last_observed_provider_timestamp=timestamp - timedelta(minutes=5),
    )
    client = Mock()
    client.get_current_image.return_value = FintrafficImageReference(
        "42", "https://images.example/a.png", timestamp
    )
    client.download.return_value = PNG_1PX
    client.downloaded_marker.return_value = '"same-etag"'

    result = process_job(
        client, fintraffic_job, dry_run=False, minimum_period_seconds=300
    )

    assert result.outcome == "unchanged"
    assert result.state_update is not None
    assert result.state_update.provider_update_timestamp == timestamp
    assert result.state_update.provider_image_marker == '"same-etag"'
    assert result.state_update.processed_timestamp is None
    assert result.period_estimate_candidate is not None


def test_country_selection_normalizes_and_deduplicates() -> None:
    assert _validate_countries(("dk", " MT ", "DK")) == ("DK", "MT")


def test_country_sample_divides_limit_between_requested_countries(monkeypatch) -> None:
    calls = []

    def fake_get_due(*args, countries, limit, **kwargs):
        calls.append((countries, limit))
        return []

    monkeypatch.setattr(
        "ingestion.windy.windy_ingestion_workflow.get_due_source_streams",
        fake_get_due,
    )

    _select_country_sample(Mock(), ("DK", "MT", "LU"), 5, timedelta())

    assert calls == [(('DK',), 2), (('MT',), 2), (('LU',), 1)]
