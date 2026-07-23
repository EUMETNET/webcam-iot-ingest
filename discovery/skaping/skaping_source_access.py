"""Authenticated access to the complete Skaping camera summary."""

from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any, Callable

import httpx


class SkapingDiscoveryError(RuntimeError):
    """Skaping could not provide a complete trustworthy discovery snapshot."""


class SkapingClient:
    def __init__(
        self,
        api_key: str,
        *,
        summary_url: str,
        timeout_s: float,
        retry_count: int,
        retry_backoff_s: float,
        minimum_camera_count: int,
        request_delay_s: float = 0,
        request_observer: Callable[[str, str, float], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Skaping API key cannot be empty")
        self._api_key = api_key
        self._summary_url = summary_url
        self._retry_count = retry_count
        self._retry_backoff_s = retry_backoff_s
        self._request_delay_s = request_delay_s
        self._minimum_camera_count = minimum_camera_count
        self._request_observer = request_observer
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

    def __enter__(self) -> "SkapingClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_cameras(self) -> list[dict[str, Any]]:
        if self._request_delay_s:
            time.sleep(self._request_delay_s)
        payload = self._request_json()
        cameras = _extract_camera_array(payload)
        if len(cameras) < self._minimum_camera_count:
            raise SkapingDiscoveryError(
                "Skaping summary camera count is below the configured "
                "complete-snapshot safety threshold"
            )
        return [dict(camera) for camera in cameras]

    def _request_json(self) -> Any:
        attempts = self._retry_count + 1
        for attempt in range(attempts):
            started = time.monotonic()
            observed = False
            try:
                response = self._client.get(
                    self._summary_url,
                    params={"api_key": self._api_key},
                )
                if (
                    response.status_code == 429
                    or response.status_code >= 500
                ) and attempt + 1 < attempts:
                    self._observe_request(
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
                self._observe_request("success", started)
                return payload
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if not observed:
                    self._observe_request(
                        "throttled" if status == 429 else "error",
                        started,
                    )
                detail = (
                    "Skaping throttled discovery (HTTP 429)"
                    if status == 429
                    else f"Skaping discovery failed with HTTP {status}"
                )
                raise SkapingDiscoveryError(detail) from error
            except (httpx.HTTPError, ValueError) as error:
                if not observed:
                    self._observe_request("error", started)
                if isinstance(error, httpx.TransportError) and attempt + 1 < attempts:
                    time.sleep(self._retry_backoff_s * 2**attempt)
                    continue
                raise SkapingDiscoveryError(
                    "Skaping discovery request failed"
                ) from error
        raise AssertionError("unreachable")

    def _observe_request(self, result: str, started: float) -> None:
        if self._request_observer is not None:
            self._request_observer(
                "summary", result, max(0.0, time.monotonic() - started)
            )

    def _wait_before_retry(
        self, response: httpx.Response, attempt: int
    ) -> None:
        retry_after = response.headers.get("Retry-After")
        try:
            requested_wait = float(retry_after) if retry_after else 0
        except ValueError:
            requested_wait = 0
        time.sleep(max(requested_wait, self._retry_backoff_s * 2**attempt))


def _extract_camera_array(payload: Any) -> list[Mapping[str, Any]]:
    """Accept known summary envelopes while requiring one complete camera list."""
    cameras: Any
    if isinstance(payload, list):
        cameras = payload
    elif isinstance(payload, Mapping):
        cameras = payload.get("cameras")
        if cameras is None:
            cameras = payload.get("data")
        if isinstance(cameras, Mapping):
            cameras = cameras.get("cameras")
    else:
        cameras = None
    if not isinstance(cameras, list):
        raise SkapingDiscoveryError(
            "Skaping summary response has no camera array"
        )
    if any(not isinstance(camera, Mapping) for camera in cameras):
        raise SkapingDiscoveryError(
            "Skaping summary contains a malformed camera"
        )
    return cameras
