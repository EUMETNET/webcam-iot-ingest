import httpx
import pytest

from ingestion.windy.windy_image_access import (
    WindyImageAccessError,
    WindyImageClient,
)


def client_for(
    handler,
    *,
    max_bytes: int = 1000,
    freshness_query_retry_count: int = 0,
    download_retry_count: int = 0,
    request_gate=None,
    observer=None,
):
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return WindyImageClient(
        "test-key",
        request_timeout_s=1,
        image_timeout_s=1,
        max_image_bytes=max_bytes,
        request_delay_s=0,
        freshness_query_retry_count=freshness_query_retry_count,
        download_retry_count=download_retry_count,
        retry_backoff_s=0,
        client=http_client,
        request_gate=request_gate,
        observer=observer,
    )


def metadata(provider_id: int = 42) -> dict:
    return {
        "webcams": [
            {
                "webcamId": provider_id,
                "lastUpdatedOn": "2026-07-21T12:00:00Z",
                "images": {
                    "current": {
                        "preview": "https://images.example/current.jpg"
                    }
                },
            }
        ]
    }


def test_refreshes_selected_rendition_with_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Windy-API-KEY"] == "test-key"
        assert request.url.params["include"] == "images"
        return httpx.Response(200, json=metadata())

    reference = client_for(handler).get_current_image("42", "preview")

    assert reference.provider_id == "42"
    assert reference.marker == "2026-07-21T12:00:00Z"
    assert reference.image_url == "https://images.example/current.jpg"


def test_refresh_observability_separates_gate_http_and_total() -> None:
    events = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=metadata())

    client_for(
        handler,
        request_gate=lambda: None,
        observer=lambda stage, outcome, duration: events.append(
            (stage, outcome, duration)
        ),
    ).get_current_image("42", "preview")

    assert [(stage, outcome) for stage, outcome, _ in events] == [
        ("provider_rate_gate_wait", "success"),
        ("provider_http", "success"),
        ("provider_refresh", "success"),
    ]
    assert all(duration >= 0 for _, _, duration in events)


@pytest.mark.parametrize(
    "payload",
    [metadata(99), {"webcams": []}, {"webcamId": 42}, []],
)
def test_rejects_inconsistent_or_incomplete_metadata(payload) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(WindyImageAccessError):
        client_for(handler).get_current_image("42", "preview")


def test_download_streams_image_without_sending_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "X-Windy-API-KEY" not in request.headers
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"abc")

    assert client_for(handler).download("https://images.example/a.jpg") == b"abc"


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"content-type": "text/html"}, b"abc"),
        ({"content-type": "image/jpeg", "content-length": "1001"}, b""),
        ({"content-type": "image/jpeg"}, b"a" * 1001),
    ],
)
def test_rejects_non_images_and_oversized_downloads(headers, content) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=content)

    with pytest.raises(WindyImageAccessError):
        client_for(handler).download("https://images.example/a.jpg")


def test_retries_throttled_metadata_request_when_configured() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=metadata())

    client_for(handler, freshness_query_retry_count=1).get_current_image(
        "42", "preview"
    )
    assert attempts == 2


def test_retries_invalid_freshness_response_when_configured() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"webcams": []})
        return httpx.Response(200, json=metadata())

    client_for(handler, freshness_query_retry_count=1).get_current_image(
        "42", "preview"
    )
    assert attempts == 2


def test_retries_failed_image_download_when_configured() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"abc"
        )

    assert (
        client_for(handler, download_retry_count=1).download(
            "https://images.example/a.jpg"
        )
        == b"abc"
    )
    assert attempts == 2


def test_rejects_malformed_metadata_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(WindyImageAccessError, match="valid JSON"):
        client_for(handler).get_current_image("42", "preview")
