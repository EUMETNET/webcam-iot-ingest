"""HTTP access and complete-snapshot validation for Fintraffic weather cameras."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any

import httpx


class FintrafficDiscoveryError(RuntimeError):
    """Fintraffic could not provide a complete, trustworthy discovery snapshot."""


class FintrafficClient:
    def __init__(
        self,
        user_header: str,
        *,
        stations_url: str,
        timeout_s: float,
        retry_count: int,
        retry_backoff_s: float,
        client: httpx.Client | None = None,
    ) -> None:
        if not user_header:
            raise ValueError("Fintraffic application identifier cannot be empty")
        self._stations_url = stations_url
        self._headers = {
            "Digitraffic-User": user_header,
            "Accept-Encoding": "gzip",
            "Accept": "application/geo+json, application/json",
        }
        self._retry_count = retry_count
        self._retry_backoff_s = retry_backoff_s
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s), headers=self._headers
        )

    def __enter__(self) -> "FintrafficClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_stations(self) -> dict[str, Any]:
        """Return one validated complete FeatureCollection."""
        attempts = self._retry_count + 1
        for attempt in range(attempts):
            try:
                response = self._client.get(
                    self._stations_url, headers=self._headers
                )
                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ) and attempt + 1 < attempts:
                    self._wait_before_retry(response, attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                detail = (
                    "Fintraffic throttled discovery (HTTP 429)"
                    if status == 429
                    else f"Fintraffic discovery failed with HTTP {status}"
                )
                raise FintrafficDiscoveryError(detail) from error
            except (httpx.HTTPError, ValueError) as error:
                if isinstance(error, httpx.TransportError) and attempt + 1 < attempts:
                    self._sleep(attempt)
                    continue
                raise FintrafficDiscoveryError(
                    "Fintraffic discovery request failed"
                ) from error
            return _validate_feature_collection(payload)
        raise AssertionError("unreachable")

    def _wait_before_retry(
        self, response: httpx.Response, attempt: int
    ) -> None:
        retry_after = response.headers.get("Retry-After")
        try:
            requested_wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            requested_wait = 0.0
        time.sleep(max(requested_wait, self._retry_backoff_s * 2**attempt))

    def _sleep(self, attempt: int) -> None:
        time.sleep(self._retry_backoff_s * 2**attempt)


def _validate_feature_collection(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise FintrafficDiscoveryError(
            "Fintraffic station response is not an object"
        )
    if payload.get("type") != "FeatureCollection":
        raise FintrafficDiscoveryError(
            "Fintraffic station response is not a FeatureCollection"
        )
    features = payload.get("features")
    if not isinstance(features, list):
        raise FintrafficDiscoveryError(
            "Fintraffic station response has no features array"
        )
    if any(not isinstance(feature, Mapping) for feature in features):
        raise FintrafficDiscoveryError(
            "Fintraffic station response contains a malformed feature"
        )
    return dict(payload)
