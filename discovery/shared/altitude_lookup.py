"""Open-Meteo elevation lookup with strict batch-response validation."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

import httpx


Coordinate = tuple[float, float]


class AltitudeLookupError(RuntimeError):
    """An elevation batch could not be retrieved or trusted."""


class AltitudeClient:
    def __init__(
        self,
        provider_url: str,
        *,
        timeout_s: float,
        request_delay_s: float = 0.1,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        if timeout_s <= 0 or request_delay_s < 0 or max_attempts < 1:
            raise ValueError("invalid altitude client retry or timeout configuration")
        self._provider_url = provider_url
        self._request_delay_s = request_delay_s
        self._max_attempts = max_attempts
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None
        self._request_count = 0

    def __enter__(self) -> "AltitudeClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def lookup(self, coordinates: Sequence[Coordinate]) -> tuple[float | None, ...]:
        if not coordinates:
            return ()
        if len(coordinates) > 100:
            raise ValueError("Open-Meteo accepts at most 100 coordinates per batch")
        for latitude, longitude in coordinates:
            if not math.isfinite(latitude) or not -90 <= latitude <= 90:
                raise ValueError("invalid latitude for altitude lookup")
            if not math.isfinite(longitude) or not -180 <= longitude <= 180:
                raise ValueError("invalid longitude for altitude lookup")

        params = {
            "latitude": ",".join(_format_coordinate(item[0]) for item in coordinates),
            "longitude": ",".join(_format_coordinate(item[1]) for item in coordinates),
        }
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            if self._request_count and self._request_delay_s:
                time.sleep(self._request_delay_s)
            self._request_count += 1
            try:
                response = self._client.get(self._provider_url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable altitude response",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return _validate_response(response.json(), len(coordinates))
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt + 1 < self._max_attempts:
                    time.sleep(min(2**attempt, 4))
        raise AltitudeLookupError("altitude lookup failed after bounded retries") from last_error


def _validate_response(payload: object, expected: int) -> tuple[float | None, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("elevation"), list):
        raise AltitudeLookupError("altitude response has no elevation array")
    values = payload["elevation"]
    if len(values) != expected:
        raise AltitudeLookupError("altitude response cardinality does not match request")
    result: list[float | None] = []
    for value in values:
        if value is None:
            result.append(None)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            altitude = float(value)
            if not math.isfinite(altitude):
                raise AltitudeLookupError("altitude response contains a non-finite value")
            result.append(altitude)
        else:
            raise AltitudeLookupError("altitude response contains a non-numeric value")
    return tuple(result)


def _format_coordinate(value: float) -> str:
    return format(value, ".10g")
