import httpx
import pytest

from discovery.skaping.skaping_source_access import (
    SkapingClient,
    SkapingDiscoveryError,
)


URL = "https://example.test/camera/summaryApp"
API_KEY = "private-test-key"


def camera(camera_id: int = 1) -> dict:
    return {"id": camera_id, "point_of_views": []}


def client_for(handler, *, retries: int = 0, minimum: int = 1) -> SkapingClient:
    return SkapingClient(
        API_KEY,
        summary_url=URL,
        timeout_s=1,
        retry_count=retries,
        retry_backoff_s=0,
        minimum_camera_count=minimum,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_fetches_summary_with_key_only_in_query_and_gzip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/camera/summaryApp"
        assert request.url.params["api_key"] == API_KEY
        assert API_KEY not in request.headers.values()
        assert "gzip" in request.headers["Accept-Encoding"]
        return httpx.Response(200, json=[camera()])

    assert client_for(handler).fetch_cameras() == [camera()]


@pytest.mark.parametrize(
    "payload",
    [
        [camera()],
        {"cameras": [camera()]},
        {"data": [camera()]},
        {"data": {"cameras": [camera()]}},
    ],
)
def test_accepts_supported_complete_summary_envelopes(payload) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    assert client_for(handler).fetch_cameras() == [camera()]


def test_retries_transient_status_then_succeeds() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json=[camera()])

    assert client_for(handler, retries=1).fetch_cameras() == [camera()]
    assert calls == 2


def test_observes_every_provider_request_attempt() -> None:
    calls = 0
    observations = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls == 1 else 200, json=[camera()])

    client = SkapingClient(
        API_KEY,
        summary_url=URL,
        timeout_s=1,
        retry_count=1,
        retry_backoff_s=0,
        minimum_camera_count=1,
        request_observer=lambda endpoint, result, duration: observations.append(
            (endpoint, result, duration)
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.fetch_cameras() == [camera()]
    assert [(endpoint, result) for endpoint, result, _ in observations] == [
        ("summary", "throttled"),
        ("summary", "success"),
    ]
    assert all(duration >= 0 for _, _, duration in observations)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": {}}, [None], {"cameras": "not-a-list"}],
)
def test_rejects_malformed_snapshot(payload) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(SkapingDiscoveryError):
        client_for(handler).fetch_cameras()


def test_rejects_snapshot_below_safety_threshold() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[camera()])

    with pytest.raises(SkapingDiscoveryError, match="safety threshold"):
        client_for(handler, minimum=2).fetch_cameras()


@pytest.mark.parametrize("status", [400, 429, 500])
def test_terminal_error_does_not_expose_response_or_key(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"private response {API_KEY}")

    with pytest.raises(SkapingDiscoveryError) as caught:
        client_for(handler).fetch_cameras()
    assert "private response" not in str(caught.value)
    assert API_KEY not in str(caught.value)
