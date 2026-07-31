from email.utils import format_datetime
from datetime import UTC, datetime

import httpx
import pytest

from ingestion.fintraffic.fintraffic_image_access import (
    FintrafficImageAccessError,
    FintrafficImageClient,
)


MARKER = "2026-07-23T10:05:00Z"


def _payload(marker=MARKER):
    return {
        "stations": [
            {
                "presets": [
                    {"id": "C0150301", "measuredTime": marker},
                    {"id": "C0150302", "measuredTime": None},
                ]
            }
        ]
    }


def _client(handler, **kwargs):
    return FintrafficImageClient(
        "test-application",
        data_url="https://example.test/stations/data",
        image_base_url="https://images.test",
        request_timeout_s=1,
        image_timeout_s=1,
        max_image_bytes=1000,
        retry_backoff_s=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_bulk_snapshot_exposes_measured_time_and_skips_null_marker():
    def handler(request):
        assert request.headers["Digitraffic-User"] == "test-application"
        return httpx.Response(200, json=_payload())

    client = _client(handler)
    assert client.refresh() == 1
    reference = client.get_current_image("C0150301", "full_jpeg")
    assert reference.marker is None
    assert reference.provider_update_timestamp == datetime(
        2026, 7, 23, 10, 5, tzinfo=UTC
    )
    assert reference.image_url == "https://images.test/C0150301.jpg"
    with pytest.raises(FintrafficImageAccessError):
        client.get_current_image("C0150302", "full_jpeg")


def test_download_validates_last_modified_against_snapshot():
    def handler(request):
        if request.url.path.endswith("/data"):
            return httpx.Response(200, json=_payload())
        return httpx.Response(
            200,
            headers={
                "content-type": "image/jpeg",
                "last-modified": format_datetime(
                    datetime(2026, 7, 23, 10, 5, tzinfo=UTC), usegmt=True
                ),
                "etag": '"fin-image-v1"',
            },
            content=b"jpeg",
        )

    client = _client(handler)
    client.refresh()
    assert client.download("https://images.test/C0150301.jpg") == b"jpeg"
    assert (
        client.downloaded_marker("https://images.test/C0150301.jpg")
        == '"fin-image-v1"'
    )


def test_download_rejects_metadata_image_race():
    def handler(request):
        if request.url.path.endswith("/data"):
            return httpx.Response(200, json=_payload())
        return httpx.Response(
            200,
            headers={
                "content-type": "image/jpeg",
                "last-modified": "Thu, 23 Jul 2026 10:06:00 GMT",
            },
            content=b"jpeg",
        )

    client = _client(handler)
    client.refresh()
    with pytest.raises(FintrafficImageAccessError):
        client.download("https://images.test/C0150301.jpg")


def test_zero_retries_means_one_metadata_and_one_image_attempt():
    attempts = {"metadata": 0, "image": 0}

    def handler(request):
        kind = "metadata" if request.url.path.endswith("/data") else "image"
        attempts[kind] += 1
        return httpx.Response(503)

    client = _client(
        handler, freshness_query_retry_count=0, download_retry_count=0
    )
    with pytest.raises(FintrafficImageAccessError):
        client.refresh()
    with pytest.raises(FintrafficImageAccessError):
        client.download("https://images.test/C0150301.jpg")
    assert attempts == {"metadata": 1, "image": 1}
