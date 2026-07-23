"""HTTP access and response validation for Windy Webcams API v3."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable

import httpx

from config.deployment_config import WindyDiscoveryArea


WINDY_WEBCAMS_URL = "https://api.windy.com/webcams/api/v3/webcams"
FREE_LISTING_MAX_RESULTS = 1000


class WindyDiscoveryError(RuntimeError):
    """Windy could not provide a complete, trustworthy discovery snapshot."""


class WindyClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float,
        page_size: int = 50,
        client: httpx.Client | None = None,
        request_delay_s: float = 0.0,
        cache_file: Path | None = None,
        request_observer: Callable[[str, str, float], None] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Windy API key cannot be empty")
        self._headers = {"X-Windy-API-KEY": api_key}
        self._page_size = page_size
        self._owns_client = client is None
        self._request_delay_s = request_delay_s
        self._made_request = False
        self._cache_file = cache_file
        self._request_observer = request_observer
        self._cache = self._load_cache()
        self._unsaved_pages = 0
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            headers={"X-Windy-API-KEY": api_key},
        )

    def close(self) -> None:
        self._save_cache(force=True)
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WindyClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def discover(self, areas: tuple[WindyDiscoveryArea, ...]) -> list[dict[str, Any]]:
        """Retrieve every configured area and deduplicate overlapping webcams."""
        webcams: dict[str, dict[str, Any]] = {}
        for area in areas:
            for webcam in self._discover_area(area):
                provider_id = _provider_id(webcam)
                # Overlapping queries can observe a metadata update at slightly
                # different times. Keep the first complete record deterministically.
                webcams.setdefault(provider_id, webcam)
        return [webcams[key] for key in sorted(webcams)]

    def discover_members(
        self,
        member_countries: tuple[str, ...],
        areas: tuple[WindyDiscoveryArea, ...],
    ) -> list[dict[str, Any]]:
        """Use country listings when complete and discs above the offset limit."""
        webcams: dict[str, dict[str, Any]] = {}
        small_countries: list[str] = []
        large_countries: set[str] = set()
        for country in member_countries:
            total, _ = self._request_page(
                {"countries": country, "limit": 1, "offset": 0, "lang": "en"}
            )
            if total <= FREE_LISTING_MAX_RESULTS:
                small_countries.append(country)
            else:
                large_countries.add(country)

        print(
            json.dumps(
                {
                    "windy_country_classification": {
                        "at_or_below_1000": small_countries,
                        "above_1000": sorted(large_countries),
                    }
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )

        covered = {country for area in areas for country in area.countries}
        missing = sorted(large_countries - covered)
        if missing:
            raise WindyDiscoveryError(
                "high-volume countries lack geographic discovery areas: "
                + ",".join(missing)
            )
        for country in small_countries:
            for webcam in self._discover_country(country):
                webcams.setdefault(_provider_id(webcam), webcam)
        large_areas = tuple(
            replace(area, countries=tuple(c for c in area.countries if c in large_countries))
            for area in areas
            if any(c in large_countries for c in area.countries)
        )
        for webcam in self.discover(large_areas):
            webcams.setdefault(_provider_id(webcam), webcam)
        return [webcams[key] for key in sorted(webcams)]

    def _discover_country(self, country: str) -> list[dict[str, Any]]:
        return self._discover_filter({"countries": country})

    def _discover_area(self, area: WindyDiscoveryArea) -> list[dict[str, Any]]:
        return self._discover_filter(
            {
                "nearby": f"{area.latitude},{area.longitude},{int(area.radius_km)}",
                "countries": ",".join(area.countries),
            }
        )

    def _discover_filter(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            params = {
                **filters,
                "limit": self._page_size,
                "offset": offset,
                "include": "categories,location",
                "lang": "en",
            }
            page_total, page = self._request_page(params)
            if total is None:
                total = page_total
            elif total != page_total:
                raise WindyDiscoveryError("Windy result total changed during pagination")
            if not page and offset < total:
                raise WindyDiscoveryError("Windy returned an incomplete paginated result")
            found.extend(page)
            offset += len(page)
        return found

    def _request_page(self, params: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        cache_key = json.dumps(params, sort_keys=True, separators=(",", ":"))
        if cache_key in self._cache["pages"]:
            return _validate_page(self._cache["pages"][cache_key])
        if self._made_request and self._request_delay_s:
            time.sleep(self._request_delay_s)
        self._made_request = True
        for attempt in range(6):
            started = time.monotonic()
            observed = False
            try:
                response = self._client.get(
                    WINDY_WEBCAMS_URL, params=params, headers=self._headers
                )
                if response.status_code == 429 and attempt < 5:
                    self._observe_request("throttled", started)
                    observed = True
                    retry_after = response.headers.get("Retry-After")
                    try:
                        requested_wait = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        requested_wait = 0.0
                    time.sleep(max(requested_wait, max(1.0, self._request_delay_s) * 2**attempt))
                    continue
                response.raise_for_status()
                payload = response.json()
                self._observe_request("success", started)
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if not observed:
                    self._observe_request(
                        "throttled" if status == 429 else "error",
                        started,
                    )
                detail = (
                    "Windy throttled discovery (HTTP 429)"
                    if status == 429
                    else f"Windy discovery failed with HTTP {status}"
                )
                raise WindyDiscoveryError(detail) from error
            except (httpx.HTTPError, ValueError) as error:
                if not observed:
                    self._observe_request("error", started)
                raise WindyDiscoveryError("Windy discovery request failed") from error
            validated = _validate_page(payload)
            self._cache["pages"][cache_key] = payload
            self._unsaved_pages += 1
            self._save_cache()
            return validated
        raise AssertionError("unreachable")

    def _observe_request(self, result: str, started: float) -> None:
        if self._request_observer is not None:
            self._request_observer(
                "list", result, max(0.0, time.monotonic() - started)
            )

    def _load_cache(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        if self._cache_file is not None and self._cache_file.exists():
            try:
                payload = json.loads(self._cache_file.read_text(encoding="utf-8"))
                if payload.get("date") == today and isinstance(payload.get("pages"), dict):
                    return payload
            except (OSError, ValueError, AttributeError):
                pass
        return {"date": today, "pages": {}}

    def _save_cache(self, *, force: bool = False) -> None:
        if self._cache_file is None or not self._unsaved_pages:
            return
        if not force and self._unsaved_pages < 25:
            return
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_file.with_suffix(self._cache_file.suffix + ".tmp")
        temporary.write_text(json.dumps(self._cache, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._cache_file)
        self._unsaved_pages = 0


def _validate_page(payload: Any) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(payload, Mapping):
        raise WindyDiscoveryError("Windy response is not a JSON object")
    total = payload.get("total")
    webcams = payload.get("webcams")
    if not isinstance(total, int) or total < 0 or not isinstance(webcams, list):
        raise WindyDiscoveryError("Windy response has an invalid result envelope")
    valid: list[dict[str, Any]] = []
    for webcam in webcams:
        if not isinstance(webcam, dict):
            raise WindyDiscoveryError("Windy response contains a malformed webcam")
        _provider_id(webcam)
        valid.append(webcam)
    return total, valid


def _provider_id(webcam: Mapping[str, Any]) -> str:
    provider_id = webcam.get("webcamId")
    if not isinstance(provider_id, (str, int)) or isinstance(provider_id, bool):
        raise WindyDiscoveryError("Windy webcam is missing a valid webcamId")
    value = str(provider_id).strip()
    if not value:
        raise WindyDiscoveryError("Windy webcam has an empty webcamId")
    return value
