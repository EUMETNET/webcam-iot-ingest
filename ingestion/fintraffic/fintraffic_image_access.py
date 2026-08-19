"""Batched Fintraffic freshness metadata and JPEG access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ingestion.windy.windy_image_access import WindyImageAccessError


class FintrafficImageAccessError(WindyImageAccessError):
    """Fintraffic metadata or image content could not be retrieved safely."""

@dataclass(frozen=True)
class FintrafficImageReference:
    provider_id: str
    image_url: str
    provider_update_timestamp: datetime


class FintrafficImageClient:
    def __init__(
        self,
        user_header: str,
        *,
        data_url: str,
        image_base_url: str,
        request_timeout_s: float,
        image_timeout_s: float,
        max_image_bytes: int,
        freshness_query_retry_count: int = 0,
        download_retry_count: int = 0,
        retry_backoff_s: float = 1,
        request_gate: Callable[[], None] | None = None,
        client: httpx.Client | None = None,
        observer: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self._headers = {
            "Digitraffic-User": user_header,
            "Accept-Encoding": "gzip",
        }
        self._data_url = data_url
        self._image_base_url = image_base_url.rstrip("/")
        self._request_timeout_s = request_timeout_s
        self._image_timeout_s = image_timeout_s
        self._max_image_bytes = max_image_bytes
        self._freshness_retries = freshness_query_retry_count
        self._download_retries = download_retry_count
        self._retry_backoff_s = retry_backoff_s
        self._observer = observer
        self._request_gate = request_gate
        self._references: dict[str, FintrafficImageReference] = {}
        self._expected_by_url: dict[str, str] = {}
        self._downloaded_etags: dict[str, str] = {}
        self._owns_client = client is None
        self._client = client or httpx.Client()

    def __enter__(self) -> "FintrafficImageClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def refresh(self) -> int:
        started = time.monotonic()
        try:
            payload = self._request_json()
            references = _parse_station_data(payload, self._image_base_url)
        except Exception:
            if self._observer:
                self._observer("provider_refresh", "failure", time.monotonic() - started)
            raise
        self._references = references
        self._expected_by_url = {
            reference.image_url: reference.provider_update_timestamp.isoformat()
            for reference in references.values()
        }
        if self._observer:
            self._observer("provider_refresh", "success", time.monotonic() - started)
        return len(references)

    def get_current_image(
        self,
        provider_id: str,
        selected_rendition: str,
        source_metadata: dict[str, object] | None = None,
    ) -> FintrafficImageReference:
        if selected_rendition != "full_jpeg":
            raise FintrafficImageAccessError("unsupported Fintraffic rendition")
        try:
            return self._references[provider_id]
        except KeyError as error:
            raise FintrafficImageAccessError(
                "Fintraffic freshness snapshot has no preset"
            ) from error

    def download(self, image_url: str) -> bytes:
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(self._download_retries + 1):
            try:
                if self._request_gate is not None:
                    self._request_gate()
                with self._client.stream(
                    "GET",
                    image_url,
                    headers={"Digitraffic-User": self._headers["Digitraffic-User"]},
                    timeout=self._image_timeout_s,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            "retryable Fintraffic image response",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    self._validate_last_modified(response, image_url)
                    etag = response.headers.get("ETag")
                    if etag is None or not etag.strip():
                        raise FintrafficImageAccessError(
                            "Fintraffic image has no ETag"
                        )
                    # Last-Modified is coherent and the ETag is now a valid
                    # provider observation, even if reading or validating the
                    # response body subsequently fails.
                    self._downloaded_etags[image_url] = etag
                    content = _read_image(response, self._max_image_bytes)
                if self._observer:
                    self._observer(
                        "source_download", "success", time.monotonic() - started
                    )
                return content
            except (httpx.HTTPError, FintrafficImageAccessError) as error:
                last_error = error
                if attempt < self._download_retries and self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * 2**attempt)
        if self._observer:
            self._observer("source_download", "failure", time.monotonic() - started)
        raise FintrafficImageAccessError(
            "Fintraffic image download failed",
            throttled=_is_throttled(last_error),
        ) from last_error

    def downloaded_marker(self, image_url: str) -> str | None:
        return self._downloaded_etags.get(image_url)

    def _request_json(self) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._freshness_retries + 1):
            started = time.monotonic()
            try:
                response = self._client.get(
                    self._data_url,
                    headers=self._headers,
                    timeout=self._request_timeout_s,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable Fintraffic metadata response",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                payload = response.json()
                if self._observer:
                    self._observer(
                        "provider_http", "success", time.monotonic() - started
                    )
                return payload
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if self._observer:
                    self._observer(
                        "provider_http", "failure", time.monotonic() - started
                    )
                if attempt < self._freshness_retries and self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * 2**attempt)
        raise FintrafficImageAccessError(
            "Fintraffic freshness query failed",
            throttled=_is_throttled(last_error),
        ) from last_error

    def _validate_last_modified(
        self, response: httpx.Response, image_url: str
    ) -> None:
        expected = self._expected_by_url.get(image_url)
        value = response.headers.get("Last-Modified")
        if expected is None or value is None:
            raise FintrafficImageAccessError(
                "Fintraffic image has no verifiable Last-Modified"
            )
        try:
            observed = parsedate_to_datetime(value).astimezone(UTC)
            expected_time = datetime.fromisoformat(expected.replace("Z", "+00:00"))
        except ValueError as error:
            raise FintrafficImageAccessError(
                "Fintraffic image has invalid Last-Modified"
            ) from error
        if observed != expected_time:
            raise FintrafficImageAccessError(
                "Fintraffic image changed after freshness snapshot"
            )


def _parse_station_data(
    payload: Any, image_base_url: str
) -> dict[str, FintrafficImageReference]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("stations"), list):
        raise FintrafficImageAccessError("invalid Fintraffic station-data snapshot")
    references: dict[str, FintrafficImageReference] = {}
    for station in payload["stations"]:
        if not isinstance(station, Mapping) or not isinstance(
            station.get("presets"), list
        ):
            raise FintrafficImageAccessError("invalid Fintraffic station data")
        for preset in station["presets"]:
            if not isinstance(preset, Mapping):
                raise FintrafficImageAccessError("invalid Fintraffic preset data")
            provider_id = preset.get("id")
            measured_time = preset.get("measuredTime")
            if measured_time is None:
                continue
            if not isinstance(provider_id, str) or not isinstance(measured_time, str):
                raise FintrafficImageAccessError("invalid Fintraffic preset timestamp")
            if provider_id in references:
                raise FintrafficImageAccessError("duplicate Fintraffic preset identifier")
            try:
                provider_timestamp = datetime.fromisoformat(
                    measured_time.replace("Z", "+00:00")
                ).astimezone(UTC)
            except ValueError as error:
                raise FintrafficImageAccessError(
                    "invalid Fintraffic preset timestamp"
                ) from error
            references[provider_id] = FintrafficImageReference(
                provider_id=provider_id,
                image_url=f"{image_base_url}/{quote(provider_id, safe='')}.jpg",
                provider_update_timestamp=provider_timestamp,
            )
    return references


def _read_image(response: httpx.Response, max_bytes: int) -> bytes:
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("image/"):
        raise FintrafficImageAccessError("Fintraffic response is not an image")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise FintrafficImageAccessError("Fintraffic image exceeds byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _is_throttled(error: Exception | None) -> bool:
    return (
        isinstance(error, httpx.HTTPStatusError)
        and error.response is not None
        and error.response.status_code == 429
    )
