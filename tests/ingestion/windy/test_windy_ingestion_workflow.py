from datetime import timedelta
from unittest.mock import Mock

from database.registry_queries import DueSourceStream
from ingestion.shared.publication_outbox import DeliveryResult
from ingestion.windy.windy_image_access import WindyImageReference
from ingestion.windy.windy_ingestion_workflow import (
    _process_job,
    _select_country_sample,
    _validate_countries,
)
from storage.s3_storage import StoredObject


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def job(*, marker: str | None = None) -> DueSourceStream:
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
        last_freshness_query_timestamp=None,
        last_download_timestamp=None,
        last_provider_image_marker=marker,
        ema_download_period=None,
    )


def test_unchanged_marker_skips_download() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "same", "https://images.example/a.jpg"
    )

    result = _process_job(Mock(), client, job(marker="same"), dry_run=True, ema_alpha=0.2)

    assert result.outcome == "unchanged"
    client.download.assert_not_called()


def test_freshness_attempt_is_recorded_when_marker_is_unchanged(monkeypatch) -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "same", "https://images.example/a.jpg"
    )
    connection = Mock()
    recorded = []
    monkeypatch.setattr(
        "ingestion.windy.windy_ingestion_workflow.record_freshness_query",
        lambda _connection, stream_id, timestamp: recorded.append(
            (stream_id, timestamp)
        ),
    )

    result = _process_job(
        connection, client, job(marker="same"), dry_run=False, ema_alpha=0.2
    )

    assert result.outcome == "unchanged"
    assert recorded[0][0] == "win42"
    connection.commit.assert_called_once_with()


def test_dry_run_decodes_image_without_database_update() -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "new", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX
    connection = Mock()

    result = _process_job(connection, client, job(), dry_run=True, ema_alpha=0.2)

    assert result.outcome == "downloaded"
    assert (result.width, result.height, result.image_format) == (1, 1, "PNG")
    connection.execute.assert_not_called()


def test_published_job_emits_latency_payload_derived_and_duration(monkeypatch) -> None:
    client = Mock()
    client.get_current_image.return_value = WindyImageReference(
        "42", "2020-01-01T00:00:00Z", "https://images.example/a.png"
    )
    client.download.return_value = PNG_1PX
    connection = Mock()
    storage = Mock(prefix="")
    storage.reference.return_value = StoredObject(
        "bucket", "T0V0/win/image.jpg", "https://objects.example/image.jpg"
    )
    events = []
    monkeypatch.setattr(
        "ingestion.windy.windy_ingestion_workflow.record_freshness_query",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "ingestion.windy.windy_ingestion_workflow.record_download_and_enqueue_publication",
        lambda *args, **kwargs: 300.0,
    )
    monkeypatch.setattr(
        "ingestion.windy.windy_ingestion_workflow.deliver_publication",
        lambda *args, **kwargs: DeliveryResult("image.jpg", "published"),
    )

    result = _process_job(
        connection,
        client,
        job(),
        dry_run=False,
        ema_alpha=0.2,
        storage=storage,
        publisher=Mock(),
        event_observer=lambda event, values: events.append((event, values)),
    )

    assert result.outcome == "published"
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
    assert sum(event == "job_completed" for event, _ in events) == 1
    assert sum(event == "derived_image" for event, _ in events) == 1
    assert sum(event == "mqtt_payload" for event, _ in events) == 1


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
