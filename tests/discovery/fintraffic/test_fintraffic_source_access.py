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


def test_fetches_and_validates_station_detail() -> None:
    detail = {
        "type": "Feature",
        "id": "C01503",
        "geometry": {"type": "Point", "coordinates": [24.0, 60.0, 0.0]},
        "properties": {
            "id": "C01503",
            "purpose": "keli",
            "presets": [
                {
                    "id": "C0150301",
                    "inCollection": True,
                    "presentationName": "Inkooseen",
                    "direction": "INCREASING_DIRECTION",
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/stations/C01503")
        return httpx.Response(200, json=detail)

    assert client_for(handler).fetch_station("C01503") == detail


def test_retries_transient_status_then_succeeds() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, json=payload())

    assert client_for(handler, retries=1).fetch_stations() == payload()
    assert calls == 2


def test_observes_list_and_detail_request_attempts() -> None:
    observations = []
    detail = {
        "type": "Feature",
        "id": "C1",
        "properties": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        response = detail if request.url.path.endswith("/C1") else payload()
        return httpx.Response(200, json=response)

    client = FintrafficClient(
        "test-application",
        stations_url=URL,
        timeout_s=1,
        retry_count=0,
        retry_backoff_s=0,
        request_delay_s=0,
        request_observer=lambda endpoint, result, duration: observations.append(
            (endpoint, result, duration)
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    client.fetch_stations()
    client.fetch_station("C1")
    assert [(endpoint, result) for endpoint, result, _ in observations] == [
        ("list", "success"),
        ("detail", "success"),
    ]
    assert all(duration >= 0 for _, _, duration in observations)


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
