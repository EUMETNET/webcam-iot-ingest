"""Compare configured geographic-disc discovery with Windy's country total."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx


URL = "https://api.windy.com/webcams/api/v3/webcams"


def request_json(
    client: httpx.Client,
    api_key: str,
    params: dict[str, Any],
    *,
    delay_s: float,
    max_retries: int = 5,
) -> dict[str, Any]:
    for attempt in range(max_retries + 1):
        response = client.get(URL, params=params, headers={"X-Windy-API-KEY": api_key})
        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Windy response is not an object")
            return payload
        if attempt == max_retries:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            requested_wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            requested_wait = 0.0
        time.sleep(max(requested_wait, delay_s * (2**attempt)))
    raise AssertionError("unreachable")


def compare(
    *,
    api_key: str,
    areas_file: Path,
    country: str,
    cache_file: Path,
    delay_s: float,
    timeout_s: float,
    bbox: tuple[float, float, float, float] | None = None,
    radius_km: int = 40,
) -> dict[str, Any]:
    if bbox is None:
        areas = json.loads(areas_file.read_text(encoding="utf-8"))
        selected = [area for area in areas if country in area["countries"]]
    else:
        selected = rectangular_grid(bbox, country=country, radius_km=radius_km)
    if not selected:
        raise ValueError(f"no configured discs include {country}")
    cache = _read_cache(cache_file)
    cached_queries = cache.setdefault("queries", {})
    request_count = 0

    with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
        country_payload = request_json(
            client,
            api_key,
            {"countries": country, "limit": 1, "lang": "en"},
            delay_s=delay_s,
        )
        request_count += 1
        country_total = _total(country_payload)

        country_ids: set[str] | None = None
        if country_total <= 1000:
            country_ids = set()
            offset = 0
            while offset < country_total:
                time.sleep(delay_s)
                payload = request_json(
                    client,
                    api_key,
                    {"countries": country, "limit": 50, "offset": offset, "lang": "en"},
                    delay_s=delay_s,
                )
                request_count += 1
                if _total(payload) != country_total:
                    raise ValueError("Windy country total changed during pagination")
                webcams = payload.get("webcams")
                if not isinstance(webcams, list) or (not webcams and offset < country_total):
                    raise ValueError("Windy returned an incomplete country page")
                country_ids.update(str(webcam["webcamId"]) for webcam in webcams)
                offset += len(webcams)

        for index, area in enumerate(selected, start=1):
            key = _area_key(area, country)
            if key in cached_queries:
                continue
            ids: list[str] = []
            offset = 0
            total: int | None = None
            while total is None or offset < total:
                time.sleep(delay_s)
                payload = request_json(
                    client,
                    api_key,
                    {
                        "nearby": f'{area["latitude"]},{area["longitude"]},{int(area["radius_km"])}',
                        "countries": country,
                        "limit": 50,
                        "offset": offset,
                        "lang": "en",
                    },
                    delay_s=delay_s,
                )
                request_count += 1
                page_total = _total(payload)
                if total is not None and total != page_total:
                    raise ValueError("Windy total changed during pagination")
                total = page_total
                webcams = payload.get("webcams")
                if not isinstance(webcams, list):
                    raise ValueError("Windy response has no webcam list")
                page_ids = [str(webcam["webcamId"]) for webcam in webcams]
                if not page_ids and offset < total:
                    raise ValueError("Windy returned an incomplete page")
                ids.extend(page_ids)
                offset += len(page_ids)
            cached_queries[key] = {"total": total, "webcam_ids": ids}
            cache["country"] = country
            cache["areas_file"] = str(areas_file)
            _write_cache(cache_file, cache)
            print(f"completed disc {index}/{len(selected)} ({total} webcams)", flush=True)

    unique_ids = {
        webcam_id
        for query in cached_queries.values()
        for webcam_id in query["webcam_ids"]
    }
    discovered = len(unique_ids)
    result = {
        "measured_at": datetime.now(UTC).isoformat(),
        "country": country,
        "radius_km": sorted({int(area["radius_km"]) for area in selected}),
        "disc_count": len(selected),
        "api_requests_this_run": request_count,
        "windy_country_total": country_total,
        "unique_webcams_from_discs": discovered,
        "difference": country_total - discovered,
        "coverage_percent": round(discovered / country_total * 100, 3) if country_total else 100.0,
        "cache_file": str(cache_file),
    }
    if country_ids is not None:
        result.update(
            {
                "country_listing_unique_ids": len(country_ids),
                "missing_from_discs": sorted(country_ids - unique_ids),
                "unexpected_from_discs": sorted(unique_ids - country_ids),
                "id_sets_equal": country_ids == unique_ids,
            }
        )
    return result


def rectangular_grid(
    bbox: tuple[float, float, float, float], *, country: str, radius_km: int
) -> list[dict[str, Any]]:
    """Cover a lat/lon bounding box with overlapping query discs."""
    south, west, north, east = bbox
    if south >= north or west >= east:
        raise ValueError("bbox must be SOUTH WEST NORTH EAST")
    # A square cell whose diagonal is below 2r is fully covered by a disc at
    # its centre.  The 0.9 factor adds margin for spherical approximations.
    spacing_km = radius_km * math.sqrt(2) * 0.9
    lat_step = spacing_km / 111.195
    mean_lat = (south + north) / 2
    lon_step = spacing_km / (111.195 * math.cos(math.radians(mean_lat)))
    rows = max(1, math.ceil((north - south) / lat_step))
    columns = max(1, math.ceil((east - west) / lon_step))
    return [
        {
            "latitude": south + (row + 0.5) * (north - south) / rows,
            "longitude": west + (column + 0.5) * (east - west) / columns,
            "radius_km": radius_km,
            "countries": [country],
        }
        for row in range(rows)
        for column in range(columns)
    ]


def _total(payload: dict[str, Any]) -> int:
    total = payload.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("Windy response contains an invalid total")
    return total


def _area_key(area: dict[str, Any], country: str) -> str:
    return f'{country}:{area["latitude"]}:{area["longitude"]}:{int(area["radius_km"])}'


def _read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"queries": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), dict):
        raise ValueError(f"invalid cache file: {path}")
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="FR", type=str.upper)
    parser.add_argument("--areas-file", type=Path, default=Path("discovery/windy/discovery_areas.json"))
    parser.add_argument("--api-key-file", type=Path, default=Path(".secrets/windy_api_key"))
    parser.add_argument("--cache", type=Path, default=Path("/tmp/windy_disc_coverage_cache.json"))
    parser.add_argument("--delay-s", type=float, default=3.0)
    parser.add_argument("--timeout-s", type=float, default=20.0)
    parser.add_argument(
        "--bbox", type=float, nargs=4, metavar=("SOUTH", "WEST", "NORTH", "EAST")
    )
    parser.add_argument("--radius-km", type=int, default=40)
    args = parser.parse_args()
    if args.delay_s < 0:
        parser.error("--delay-s cannot be negative")
    result = compare(
        api_key=args.api_key_file.read_text(encoding="utf-8").strip(),
        areas_file=args.areas_file,
        country=args.country,
        cache_file=args.cache,
        delay_s=args.delay_s,
        timeout_s=args.timeout_s,
        bbox=tuple(args.bbox) if args.bbox else None,
        radius_km=args.radius_km,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
