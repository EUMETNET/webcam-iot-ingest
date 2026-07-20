import httpx
import pytest

from config.deployment_config import WindyDiscoveryArea
from discovery.windy.windy_source_access import WindyClient, WindyDiscoveryError


AREA = WindyDiscoveryArea(60.0, 25.0, 100.0, ("FI",))


def client_for(handler) -> WindyClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return WindyClient("test-key", timeout_s=1, page_size=2, client=http_client)


def webcam(provider_id: int, title: str = "Outdoor") -> dict:
    return {
        "webcamId": provider_id,
        "title": title,
        "location": {
            "latitude": 60.0,
            "longitude": 25.0,
            "countryCode": "FI",
        },
    }


def test_paginates_and_authenticates() -> None:
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Windy-API-KEY"] == "test-key"
        assert request.url.params["nearby"] == "60.0,25.0,100"
        offsets.append(int(request.url.params["offset"]))
        page = [webcam(1), webcam(2)] if offsets[-1] == 0 else [webcam(3)]
        return httpx.Response(200, json={"total": 3, "webcams": page})

    assert [item["webcamId"] for item in client_for(handler).discover((AREA,))] == [1, 2, 3]
    assert offsets == [0, 2]


def test_deduplicates_overlapping_areas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 1, "webcams": [webcam(7)]})

    assert len(client_for(handler).discover((AREA, AREA))) == 1


def test_member_discovery_uses_country_listing_below_offset_limit_and_discs_above() -> None:
    france_area = WindyDiscoveryArea(46.0, 2.0, 40.0, ("FR",))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = request.url.params
        country = params["countries"]
        if params["limit"] == "1":
            return httpx.Response(
                200, json={"total": 3 if country == "BE" else 1001, "webcams": []}
            )
        if "nearby" in params:
            item = webcam(9)
            item["location"]["countryCode"] = "FR"
            return httpx.Response(200, json={"total": 1, "webcams": [item]})
        offset = int(params["offset"])
        page = [webcam(1), webcam(2)] if offset == 0 else [webcam(3)]
        for item in page:
            item["location"]["countryCode"] = "BE"
        return httpx.Response(200, json={"total": 3, "webcams": page})

    result = client_for(handler).discover_members(("BE", "FR"), (france_area,))

    assert {item["webcamId"] for item in result} == {1, 2, 3, 9}
    nearby = [request for request in requests if "nearby" in request.url.params]
    assert len(nearby) == 1
    assert nearby[0].url.params["countries"] == "FR"


def test_member_discovery_fails_when_large_country_has_no_discs() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 1001, "webcams": []})

    with pytest.raises(WindyDiscoveryError, match="DE"):
        client_for(handler).discover_members(("DE",), (AREA,))


@pytest.mark.parametrize("status", [429, 500])
def test_reports_http_errors_without_response_body_or_key(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="secret provider response")

    with pytest.raises(WindyDiscoveryError) as caught:
        client_for(handler).discover((AREA,))
    assert "secret provider response" not in str(caught.value)
    assert "test-key" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [[], {"total": "1", "webcams": []}, {"total": 1, "webcams": []}],
)
def test_rejects_malformed_or_partial_responses(payload) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(WindyDiscoveryError):
        client_for(handler).discover((AREA,))
