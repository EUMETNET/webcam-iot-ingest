"""ETag-based Skaping freshness checks and mini-image downloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ingestion.shared.provider_access import ProviderImageAccessError


class SkapingImageAccessError(ProviderImageAccessError):
    """Skaping freshness metadata or image content is unusable."""


@dataclass(frozen=True)
class SkapingImageReference:
    provider_id: str
    marker: str
    image_url: str
    provider_update_timestamp: datetime | None
    resolved_target_path: str


class SkapingImageClient:
    def __init__(
        self,
        *,
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
        self._request_timeout_s = request_timeout_s
        self._image_timeout_s = image_timeout_s
        self._max_image_bytes = max_image_bytes
        self._freshness_retries = freshness_query_retry_count
        self._download_retries = download_retry_count
        self._retry_backoff_s = retry_backoff_s
        self._request_gate = request_gate
        self._observer = observer
        self._expected_etags: dict[str, str] = {}
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=True)

    def __enter__(self) -> "SkapingImageClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def get_current_image(
        self,
        provider_id: str,
        selected_rendition: str,
        source_metadata: dict[str, object] | None = None,
    ) -> SkapingImageReference:
        if selected_rendition != "mini":
            raise SkapingImageAccessError("unsupported Skaping rendition")
        latest_media = (
            source_metadata.get("latest_media")
            if isinstance(source_metadata, dict)
            else None
        )
        image_url = (
            latest_media.get("mini") if isinstance(latest_media, dict) else None
        )
        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            raise SkapingImageAccessError("Skaping mini pointer is unavailable")
        started = time.monotonic()
        try:
            response = self._request_with_retries(
                "HEAD",
                image_url,
                timeout=self._request_timeout_s,
                retries=self._freshness_retries,
            )
            etag = response.headers.get("ETag")
            if not etag:
                raise SkapingImageAccessError("Skaping image has no ETag")
            final_url = str(response.url)
            provider_timestamp = _last_modified(response.headers.get("Last-Modified"))
            self._expected_etags[final_url] = etag
        except Exception:
            if self._observer:
                self._observer(
                    "provider_refresh", "failure", time.monotonic() - started
                )
            raise
        if self._observer:
            self._observer(
                "provider_refresh", "success", time.monotonic() - started
            )
        return SkapingImageReference(
            provider_id=provider_id,
            marker=etag,
            image_url=final_url,
            provider_update_timestamp=provider_timestamp,
            resolved_target_path=urlsplit(final_url).path,
        )

    def download(self, image_url: str) -> bytes:
        started = time.monotonic()
        last_error: Exception | None = None
        try:
            for attempt in range(self._download_retries + 1):
                try:
                    content = self._download_once(image_url)
                    break
                except (httpx.HTTPError, SkapingImageAccessError) as error:
                    last_error = error
                    if attempt >= self._download_retries:
                        raise
                    if self._retry_backoff_s:
                        time.sleep(self._retry_backoff_s * 2**attempt)
        except Exception:
            if self._observer:
                self._observer(
                    "source_download", "failure", time.monotonic() - started
                )
            raise SkapingImageAccessError(
                "Skaping image download failed",
                throttled=(
                    isinstance(last_error, httpx.HTTPStatusError)
                    and last_error.response.status_code == 429
                ),
            ) from last_error
        if self._observer:
            self._observer("source_download", "success", time.monotonic() - started)
        return content

    def _download_once(self, image_url: str) -> bytes:
        if self._request_gate is not None:
            self._request_gate()
        with self._client.stream(
            "GET", image_url, timeout=self._image_timeout_s
        ) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    "retryable Skaping image response",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            expected = self._expected_etags.get(image_url)
            if expected is None or response.headers.get("ETag") != expected:
                raise SkapingImageAccessError(
                    "Skaping image changed after freshness check"
                )
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/"):
                raise SkapingImageAccessError("Skaping response is not an image")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self._max_image_bytes:
                    raise SkapingImageAccessError(
                        "Skaping image exceeds byte limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        retries: int,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if self._request_gate is not None:
                    self._request_gate()
                response = self._client.request(method, url, timeout=timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable Skaping response",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, SkapingImageAccessError) as error:
                last_error = error
                if attempt < retries and self._retry_backoff_s:
                    time.sleep(self._retry_backoff_s * 2**attempt)
        raise SkapingImageAccessError(
            "Skaping request failed",
            throttled=(
                isinstance(last_error, httpx.HTTPStatusError)
                and last_error.response.status_code == 429
            ),
        ) from last_error


def _last_modified(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
