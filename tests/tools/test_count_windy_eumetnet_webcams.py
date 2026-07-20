import httpx

from tools.count_windy_eumetnet_webcams import (
    EUMETNET_COUNTRY_CODES,
    count_webcams,
    country_groups,
)


def test_country_groups_cover_33_members_once() -> None:
    groups = country_groups()
    assert [len(group) for group in groups] == [10, 10, 10, 3]
    assert tuple(code for group in groups for code in group) == EUMETNET_COUNTRY_CODES
    assert len(set(EUMETNET_COUNTRY_CODES)) == 33


def test_count_uses_four_disjoint_total_only_queries() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"total": len(requests) * 100, "webcams": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = count_webcams("key", delay_s=0, client=client)

    assert result["total"] == 1000
    assert result["request_count"] == 4
    assert len(requests) == 4
    assert all(request.url.params["limit"] == "1" for request in requests)
    assert all(len(request.url.params["countries"].split(",")) <= 10 for request in requests)
    assert all(request.headers["X-Windy-API-KEY"] == "key" for request in requests)


def test_count_retries_throttled_group() -> None:
    statuses = iter([429, 200, 200, 200, 200])

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"total": 7, "webcams": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = count_webcams("key", delay_s=0, client=client)

    assert result["total"] == 28
