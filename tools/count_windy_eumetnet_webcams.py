"""Count Windy webcams in the 33 EUMETNET member countries.

The Windy API accepts at most ten country filters in one request.  This tool
uses four disjoint groups and reads only the result totals, so a complete count
costs four API requests rather than paginating through every webcam.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from typing import Any

import httpx


WINDY_WEBCAMS_URL = "https://api.windy.com/webcams/api/v3/webcams"


# Full members published by EUMETNET (February 2025 list); cooperating members
# are intentionally excluded.
EUMETNET_COUNTRY_CODES = (
    "AT", "BE", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IS", "IE", "IT", "LV", "LU", "ME", "NL", "LT",
    "MT", "NO", "PL", "PT", "RO", "RS", "SK", "SI", "ES", "SE",
    "CH", "MK", "GB",
)
COUNTRIES_PER_REQUEST = 10


def country_groups() -> tuple[tuple[str, ...], ...]:
    return tuple(
        EUMETNET_COUNTRY_CODES[index:index + COUNTRIES_PER_REQUEST]
        for index in range(0, len(EUMETNET_COUNTRY_CODES), COUNTRIES_PER_REQUEST)
    )


def count_webcams(
    api_key: str,
    *,
    delay_s: float = 2.0,
    timeout_s: float = 15.0,
    max_retries: int = 5,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Return group totals while pacing calls and respecting HTTP 429."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(timeout_s))
    groups: list[dict[str, Any]] = []
    try:
        for group_index, countries in enumerate(country_groups()):
            if group_index:
                time.sleep(delay_s)
            payload = _request_total(
                http_client,
                api_key,
                countries,
                max_retries=max_retries,
                delay_s=delay_s,
            )
            groups.append({"countries": list(countries), "total": payload})
    finally:
        if owns_client:
            http_client.close()

    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "member_country_count": len(EUMETNET_COUNTRY_CODES),
        "request_count": len(groups),
        "total": sum(group["total"] for group in groups),
        "groups": groups,
    }


def count_by_country(
    api_key: str, *, delay_s: float = 2.0, timeout_s: float = 15.0
) -> dict[str, Any]:
    totals: dict[str, int] = {}
    with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
        for index, country in enumerate(EUMETNET_COUNTRY_CODES):
            if index:
                time.sleep(delay_s)
            totals[country] = _request_total(
                client, api_key, (country,), max_retries=5, delay_s=delay_s
            )
            print(f"{country}: {totals[country]}", flush=True)
    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "member_country_count": len(totals),
        "request_count": len(totals),
        "total": sum(totals.values()),
        "countries": totals,
    }


def _request_total(
    client: httpx.Client,
    api_key: str,
    countries: tuple[str, ...],
    *,
    max_retries: int,
    delay_s: float,
) -> int:
    for attempt in range(max_retries + 1):
        response = client.get(
            WINDY_WEBCAMS_URL,
            params={"countries": ",".join(countries), "limit": 1, "lang": "en"},
            headers={"X-Windy-API-KEY": api_key},
        )
        if response.status_code != 429:
            response.raise_for_status()
            payload = response.json()
            total = payload.get("total") if isinstance(payload, dict) else None
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("Windy response contains an invalid total")
            return total
        if attempt == max_retries:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            wait_s = float(retry_after) if retry_after is not None else 0.0
        except ValueError:
            wait_s = 0.0
        time.sleep(max(wait_s, delay_s * (2**attempt)))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key-file", type=Path, default=Path(".secrets/windy_api_key")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delay-s", type=float, default=2.0)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument(
        "--per-country", action="store_true", help="make one request per member"
    )
    args = parser.parse_args()
    if args.delay_s < 0:
        parser.error("--delay-s cannot be negative")

    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    counter = count_by_country if args.per_country else count_webcams
    result = counter(api_key, delay_s=args.delay_s, timeout_s=args.timeout_s)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
