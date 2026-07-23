import httpx
import pytest

from discovery.fintraffic.fintraffic_source_access import (
    FintrafficClient,
    FintrafficDiscoveryError,
)


URL = "https://example.test/api/weathercam/v1/stations"


def payload() -> dict:
    return {"type": "FeatureCollection", "features": []}


def client_for(handler, *, retries: int = 0) -> FintrafficClient:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return FintrafficClient(
        "test-application",
        stations_url=URL,
        timeout_s=1,
        retry_count=retries,
        retry_backoff_s=0,
        client=client,
    )


def test_fetches_complete_snapshot_with_required_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == URL
        assert request.headers["Digitraffic-User"] == "test-application"
        assert "gzip" in request.headers["Accept-Encoding"]
        return httpx.Response(200, json=payload())

    assert client_for(handler).fetch_stations() == payload()


def test_retries_transient_status_then_succeeds() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json=payload())

    assert client_for(handler, retries=1).fetch_stations() == payload()
    assert calls == 2


@pytest.mark.parametrize(
    "bad_payload",
    [[], {}, {"type": "Feature", "features": []}, {"type": "FeatureCollection"}],
)
def test_rejects_malformed_or_partial_snapshot(bad_payload) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_payload)

    with pytest.raises(FintrafficDiscoveryError):
        client_for(handler).fetch_stations()


@pytest.mark.parametrize("status", [400, 429, 500])
def test_terminal_error_does_not_expose_response_or_header(status: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="private response body")

    with pytest.raises(FintrafficDiscoveryError) as caught:
        client_for(handler).fetch_stations()
    assert "private response body" not in str(caught.value)
    assert "test-application" not in str(caught.value)
