"""Windy webcam refresh and tokenized source-image download access."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import time
import threading
from typing import Any, Callable, Mapping, Sequence

import httpx

from ingestion.shared.provider_access import ProviderImageAccessError


WEBCAM_URL = "https://api.windy.com/webcams/api/v3/webcams/{webcam_id}"
WEBCAMS_URL = "https://api.windy.com/webcams/api/v3/webcams"
WINDY_WEBCAM_IDS_LIMIT = 50


class WindyImageAccessError(ProviderImageAccessError):
    """Windy metadata or image content could not be retrieved safely."""

    def __init__(self, message: str, *, throttled: bool = False) -> None:
        super().__init__(message)
        self.throttled = throttled


@dataclass(frozen=True)
class WindyImageReference:
    provider_id: str
    marker: str
    image_url: str


@dataclass(frozen=True)
class WindyBatchFreshnessResult:
    requested_streams: int
    returned_streams: int
    missing_streams: int
    successful_requests: int
    failed_requests: int
    throttled_requests: int


@dataclass(frozen=True)
class _CachedFreshnessError:
    message: str
    throttled: bool = False


class WindyImageClient:
    def __init__(
        self,
        api_key: str,
        *,
        request_timeout_s: float,
        image_timeout_s: float,
        max_image_bytes: int,
        request_delay_s: float = 0.1,
        freshness_query_retry_count: int = 1,
        download_retry_count: int = 1,
        retry_backoff_s: float = 1,
        client: httpx.Client | None = None,
        request_gate: Callable[[], None] | None = None,
        observer: Callable[[str, str, float], None] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Windy API key cannot be empty")
        if request_timeout_s <= 0 or image_timeout_s <= 0 or max_image_bytes < 1:
            raise ValueError("invalid Windy image timeout or size configuration")
        if (
            request_delay_s < 0
            or freshness_query_retry_count < 0
            or download_retry_count < 0
            or retry_backoff_s < 0
        ):
            raise ValueError("invalid Windy image retry configuration")
        self._headers = {"X-Windy-API-KEY": api_key}
        self._request_timeout_s = request_timeout_s
        self._image_timeout_s = image_timeout_s
        self._max_image_bytes = max_image_bytes
        self._request_delay_s = request_delay_s
        self._freshness_query_retry_count = freshness_query_retry_count
        self._download_retry_count = download_retry_count
        self._retry_backoff_s = retry_backoff_s
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._metadata_requests = 0
        self._request_lock = threading.Lock()
        self._request_gate = request_gate
        self._observer = observer
        self._references: dict[
            str, WindyImageReference | _CachedFreshnessError
        ] = {}
        self._batch_refreshed = False

    def __enter__(self) -> "WindyImageClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def get_current_image(
        self,
        provider_id: str,
        selected_rendition: str,
        source_metadata: dict[str, object] | None = None,
    ) -> WindyImageReference:
        if self._batch_refreshed:
            cached = self._references.get(provider_id)
            if isinstance(cached, WindyImageReference):
                return cached
            if isinstance(cached, _CachedFreshnessError):
                raise WindyImageAccessError(
                    cached.message, throttled=cached.throttled
                )
            raise WindyImageAccessError(
                "Windy batched freshness result is unavailable"
            )
        started = time.monotonic()
        for attempt in range(self._freshness_query_retry_count + 1):
            try:
                result = self._get_current_image(provider_id, selected_rendition)
            except Exception:
                if attempt < self._freshness_query_retry_count:
                    if self._retry_backoff_s:
                        time.sleep(self._retry_backoff_s * (2**attempt))
                    continue
                if self._observer is not None:
                    self._observer(
                        "provider_refresh", "failure", time.monotonic() - started
                    )
                raise
            if self._observer is not None:
                self._observer(
                    "provider_refresh", "success", time.monotonic() - started
                )
            return result
        raise AssertionError("unreachable")

    def refresh(
        self,
        requests: Sequence[tuple[str, str]],
        *,
        max_workers: int,
    ) -> WindyBatchFreshnessResult:
        """Resolve up to 50 registered IDs per request without downloading images."""
        if max_workers < 1:
            raise ValueError("batch freshness workers must be positive")
        if self._batch_refreshed:
            raise RuntimeError("Windy batch freshness snapshot is already loaded")
        if len({provider_id for provider_id, _ in requests}) != len(requests):
            raise ValueError("Windy batch freshness provider IDs must be unique")
        started = time.monotonic()
        cache: dict[str, WindyImageReference | _CachedFreshnessError] = {}
        batches = [
            requests[offset : offset + WINDY_WEBCAM_IDS_LIMIT]
            for offset in range(0, len(requests), WINDY_WEBCAM_IDS_LIMIT)
        ]
        successful_requests = 0
        failed_requests = 0
        throttled_requests = 0
        try:
            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="windy-freshness"
            ) as executor:
                futures = {
                    executor.submit(self._get_current_image_batch, batch): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch = futures[future]
                    try:
                        references = future.result()
                        successful_requests += 1
                    except WindyImageAccessError as error:
                        failed_requests += 1
                        throttled_requests += int(error.throttled)
                        for provider_id, _ in batch:
                            cache[provider_id] = _CachedFreshnessError(
                                "Windy batched freshness request failed",
                                throttled=error.throttled,
                            )
                        continue
                    for provider_id, _ in batch:
                        cache[provider_id] = references.get(
                            provider_id,
                            _CachedFreshnessError(
                                "Windy webcam is absent from batched freshness response"
                            ),
                        )
        except Exception:
            if self._observer is not None:
                self._observer(
                    "provider_refresh",
                    "failure",
                    time.monotonic() - started,
                )
            raise
        self._references = cache
        self._batch_refreshed = True
        returned_streams = sum(
            isinstance(value, WindyImageReference) for value in cache.values()
        )
        if self._observer is not None:
            self._observer(
                "provider_refresh",
                "success",
                time.monotonic() - started,
            )
        return WindyBatchFreshnessResult(
            requested_streams=len(requests),
            returned_streams=returned_streams,
            missing_streams=len(requests) - returned_streams,
            successful_requests=successful_requests,
            failed_requests=failed_requests,
            throttled_requests=throttled_requests,
        )

    def _get_current_image_batch(
        self, requests: Sequence[tuple[str, str]]
    ) -> dict[str, WindyImageReference]:
        for attempt in range(self._freshness_query_retry_count + 1):
            try:
                return self._get_current_image_batch_once(requests)
            except WindyImageAccessError:
                if attempt >= self._freshness_query_retry_count:
                    raise
                if self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * 2**attempt)
        raise AssertionError("unreachable")

    def _get_current_image_batch_once(
        self, requests: Sequence[tuple[str, str]]
    ) -> dict[str, WindyImageReference]:
        if not requests or len(requests) > WINDY_WEBCAM_IDS_LIMIT:
            raise ValueError("Windy freshness batches must contain 1 to 50 IDs")
        provider_ids = [provider_id for provider_id, _ in requests]
        selected_renditions = dict(requests)
        if any(not provider_id or not rendition for provider_id, rendition in requests):
            raise ValueError("provider ID and selected rendition are required")
        self._wait_for_request_gate()
        response = self._request(
            WEBCAMS_URL,
            params={
                "webcamIds": ",".join(provider_ids),
                "include": "images",
                "lang": "en",
                "limit": str(WINDY_WEBCAM_IDS_LIMIT),
            },
            headers=self._headers,
            timeout=self._request_timeout_s,
            operation="batched metadata refresh",
            retry_count=0,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise WindyImageAccessError(
                "Windy batched metadata response is not valid JSON"
            ) from error
        webcams = _extract_webcam_list(payload)
        requested = set(provider_ids)
        references: dict[str, WindyImageReference] = {}
        for webcam in webcams:
            returned_id = str(webcam.get("webcamId", "")).strip()
            if returned_id not in requested or returned_id in references:
                raise WindyImageAccessError(
                    "Windy batched metadata returned inconsistent identifiers"
                )
            references[returned_id] = _reference_from_webcam(
                webcam, returned_id, selected_renditions[returned_id]
            )
        return references

    def _get_current_image(
        self, provider_id: str, selected_rendition: str
    ) -> WindyImageReference:
        if not provider_id or not selected_rendition:
            raise ValueError("provider ID and selected rendition are required")
        if self._request_gate is not None:
            self._wait_for_request_gate()
            response = self._request(
                WEBCAM_URL.format(webcam_id=provider_id),
                params={"include": "images", "lang": "en"},
                headers=self._headers,
                timeout=self._request_timeout_s,
                operation="metadata refresh",
                retry_count=0,
            )
        else:
            # Standalone callers retain their local pacing. Worker callers use
            # the shared RateGate and may safely overlap requests through httpx.
            with self._request_lock:
                if self._metadata_requests and self._request_delay_s:
                    time.sleep(self._request_delay_s)
                self._metadata_requests += 1
                response = self._request(
                    WEBCAM_URL.format(webcam_id=provider_id),
                    params={"include": "images", "lang": "en"},
                    headers=self._headers,
                    timeout=self._request_timeout_s,
                    operation="metadata refresh",
                    retry_count=0,
                )
        try:
            payload = response.json()
        except ValueError as error:
            raise WindyImageAccessError(
                "Windy metadata response is not valid JSON"
            ) from error
        webcam = _extract_webcam(payload)
        returned_id = str(webcam.get("webcamId", "")).strip()
        if returned_id != provider_id:
            raise WindyImageAccessError("Windy returned a different webcam identifier")
        return _reference_from_webcam(webcam, provider_id, selected_rendition)

    def _wait_for_request_gate(self) -> None:
        if self._request_gate is None:
            return
        gate_started = time.monotonic()
        try:
            self._request_gate()
        except Exception:
            if self._observer is not None:
                self._observer(
                    "provider_rate_gate_wait",
                    "failure",
                    time.monotonic() - gate_started,
                )
            raise
        if self._observer is not None:
            self._observer(
                "provider_rate_gate_wait",
                "success",
                time.monotonic() - gate_started,
            )

    def download(self, image_url: str) -> bytes:
        started = time.monotonic()
        try:
            result = self._download(image_url)
        except Exception:
            if self._observer is not None:
                self._observer("source_download", "failure", time.monotonic() - started)
            raise
        if self._observer is not None:
            self._observer("source_download", "success", time.monotonic() - started)
        return result

    def _download(self, image_url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self._download_retry_count + 1):
            try:
                with self._client.stream(
                    "GET", image_url, timeout=self._image_timeout_s
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            "retryable Windy response",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    _validate_image_headers(response, self._max_image_bytes)
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self._max_image_bytes:
                            raise WindyImageAccessError(
                                "Windy image exceeds byte limit"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except WindyImageAccessError:
                raise
            except httpx.HTTPError as error:
                last_error = error
                if attempt < self._download_retry_count and self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * (2**attempt))
        raise WindyImageAccessError(
            "Windy image download failed", throttled=_is_throttled(last_error)
        ) from last_error

    def _request(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
        operation: str,
        retry_count: int,
    ) -> httpx.Response:
        started = time.monotonic()
        try:
            response = self._request_with_retries(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                operation=operation,
                retry_count=retry_count,
            )
        except Exception:
            if self._observer is not None:
                self._observer("provider_http", "failure", time.monotonic() - started)
            raise
        if self._observer is not None:
            self._observer("provider_http", "success", time.monotonic() - started)
        return response

    def _request_with_retries(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        timeout: float,
        operation: str,
        retry_count: int,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(retry_count + 1):
            try:
                response = self._client.get(
                    url, params=params, headers=headers, timeout=timeout
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable Windy response",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except httpx.HTTPError as error:
                last_error = error
                if attempt < retry_count and self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * (2**attempt))
        raise WindyImageAccessError(
            f"Windy {operation} failed", throttled=_is_throttled(last_error)
        ) from last_error


def _is_throttled(error: Exception | None) -> bool:
    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response is not None
        and error.response.status_code == 429
    )


def _extract_webcam(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise WindyImageAccessError("Windy metadata response is not an object")
    if "webcamId" in payload:
        return payload
    webcams = payload.get("webcams")
    if not isinstance(webcams, list) or len(webcams) != 1 or not isinstance(webcams[0], Mapping):
        raise WindyImageAccessError("Windy metadata response has no unique webcam")
    return webcams[0]


def _extract_webcam_list(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise WindyImageAccessError(
            "Windy batched metadata response is not an object"
        )
    webcams = payload.get("webcams")
    if not isinstance(webcams, list) or any(
        not isinstance(webcam, Mapping) for webcam in webcams
    ):
        raise WindyImageAccessError(
            "Windy batched metadata response has an invalid webcam list"
        )
    return webcams


def _reference_from_webcam(
    webcam: Mapping[str, Any],
    provider_id: str,
    selected_rendition: str,
) -> WindyImageReference:
    marker = webcam.get("lastUpdatedOn")
    if not isinstance(marker, str) or not marker.strip():
        raise WindyImageAccessError("Windy image marker is missing")
    images = webcam.get("images")
    current = images.get("current") if isinstance(images, Mapping) else None
    rendition = (
        current.get(selected_rendition) if isinstance(current, Mapping) else None
    )
    if isinstance(rendition, Mapping):
        rendition = rendition.get("url")
    if not isinstance(rendition, str) or not rendition.startswith("https://"):
        raise WindyImageAccessError("Windy selected rendition is unavailable")
    return WindyImageReference(provider_id, marker.strip(), rendition)


def _validate_image_headers(response: httpx.Response, max_bytes: int) -> None:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise WindyImageAccessError("Windy image exceeds byte limit")
        except ValueError as error:
            raise WindyImageAccessError(
                "Windy image content length is invalid"
            ) from error
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("image/"):
        raise WindyImageAccessError("Windy response is not an image")
