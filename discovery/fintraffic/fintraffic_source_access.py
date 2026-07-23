"""HTTP access and complete-snapshot validation for Fintraffic weather cameras."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any, Callable
from urllib.parse import quote

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
        request_delay_s: float = 0.1,
        request_observer: Callable[[str, str, float], None] | None = None,
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
        self._request_delay_s = request_delay_s
        self._request_observer = request_observer
        self._made_request = False
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
        return _validate_feature_collection(self._request_json(self._stations_url))

    def fetch_station(self, station_id: str) -> dict[str, Any]:
        """Return one validated detailed station feature."""
        url = f"{self._stations_url.rstrip('/')}/{quote(station_id, safe='')}"
        payload = self._request_json(url)
        if not isinstance(payload, Mapping) or payload.get("type") != "Feature":
            raise FintrafficDiscoveryError(
                f"Fintraffic station {station_id} detail is not a Feature"
            )
        if str(payload.get("id")) != station_id:
            raise FintrafficDiscoveryError(
                f"Fintraffic station {station_id} detail has a mismatched identifier"
            )
        if not isinstance(payload.get("properties"), Mapping):
            raise FintrafficDiscoveryError(
                f"Fintraffic station {station_id} detail has invalid properties"
            )
        return dict(payload)

    def _request_json(self, url: str) -> Any:
        if self._made_request and self._request_delay_s:
            time.sleep(self._request_delay_s)
        self._made_request = True
        attempts = self._retry_count + 1
        for attempt in range(attempts):
            started = time.monotonic()
            observed = False
            try:
                response = self._client.get(
                    url, headers=self._headers
                )
                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ) and attempt + 1 < attempts:
                    self._observe_request(
                        url,
                        "throttled"
                        if response.status_code == 429
                        else "error",
                        started,
                    )
                    observed = True
                    self._wait_before_retry(response, attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
                self._observe_request(url, "success", started)
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if not observed:
                    self._observe_request(
                        url,
                        "throttled" if status == 429 else "error",
                        started,
                    )
                detail = (
                    "Fintraffic throttled discovery (HTTP 429)"
                    if status == 429
                    else f"Fintraffic discovery failed with HTTP {status}"
                )
                raise FintrafficDiscoveryError(detail) from error
            except (httpx.HTTPError, ValueError) as error:
                if not observed:
                    self._observe_request(url, "error", started)
                if isinstance(error, httpx.TransportError) and attempt + 1 < attempts:
                    self._sleep(attempt)
                    continue
                raise FintrafficDiscoveryError(
                    "Fintraffic discovery request failed"
                ) from error
            return payload
        raise AssertionError("unreachable")

    def _observe_request(
        self, url: str, result: str, started: float
    ) -> None:
        if self._request_observer is not None:
            endpoint_type = (
                "list"
                if url.rstrip("/") == self._stations_url.rstrip("/")
                else "detail"
            )
            self._request_observer(
                endpoint_type,
                result,
                max(0.0, time.monotonic() - started),
            )

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
