import httpx
import pytest

from discovery.shared.altitude_lookup import AltitudeClient, AltitudeLookupError


def test_lookup_preserves_order_and_accepts_zero_negative_and_null() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["latitude"] == "60,61,62"
        assert request.url.params["longitude"] == "24,25,26"
        return httpx.Response(200, json={"elevation": [0, -4.5, None]})

    client = AltitudeClient(
        "https://example.test/v1/elevation",
        timeout_s=1,
        request_delay_s=0,
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.lookup([(60, 24), (61, 25), (62, 26)]) == (0.0, -4.5, None)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"elevation": [1]},
        {"elevation": [True, 2]},
        {"elevation": [float("inf"), 2]},
    ],
)
def test_lookup_rejects_untrustworthy_responses(payload: object) -> None:
    client = AltitudeClient(
        "https://example.test/v1/elevation",
        timeout_s=1,
        request_delay_s=0,
        max_attempts=1,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ),
    )

    with pytest.raises(AltitudeLookupError):
        client.lookup([(60, 24), (61, 25)])


def test_lookup_retries_throttling_with_bounded_attempts() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": True})
        return httpx.Response(200, json={"elevation": [42]})

    client = AltitudeClient(
        "https://example.test/v1/elevation",
        timeout_s=1,
        request_delay_s=0,
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.lookup([(60, 24)]) == (42.0,)
    assert attempts == 2


def test_lookup_rejects_more_than_provider_batch_limit() -> None:
    client = AltitudeClient(
        "https://example.test/v1/elevation",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    with pytest.raises(ValueError, match="at most 100"):
        client.lookup([(60, 24)] * 101)
