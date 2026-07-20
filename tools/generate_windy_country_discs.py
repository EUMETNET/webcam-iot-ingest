"""Generate conservative Windy query discs from open country boundaries."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import box, shape


ISO3 = {
    "AT": "AUT", "CH": "CHE", "CZ": "CZE", "DE": "DEU",
    "ES": "ESP", "GB": "GBR", "IT": "ITA", "NO": "NOR",
}
API = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/ADM0/"


def load_boundary(country: str, client: httpx.Client) -> tuple[Any, str]:
    metadata_response = client.get(API.format(iso3=ISO3[country]))
    metadata_response.raise_for_status()
    metadata = metadata_response.json()
    url = metadata["simplifiedGeometryGeoJSON"]
    boundary_response = client.get(url)
    boundary_response.raise_for_status()
    payload = boundary_response.json()
    geometry = payload["features"][0]["geometry"]
    return shape(geometry), url


def generate_discs(geometry: Any, country: str, radius_km: int) -> list[dict[str, Any]]:
    spacing_km = radius_km * math.sqrt(2) * 0.85
    lat_step = spacing_km / 111.195
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    centers: set[tuple[float, float]] = set()
    for polygon in polygons:
        west, south, east, north = polygon.bounds
        rows = max(1, math.ceil((north - south) / lat_step))
        for row in range(rows):
            cell_south = south + row * (north - south) / rows
            cell_north = south + (row + 1) * (north - south) / rows
            latitude = (cell_south + cell_north) / 2
            lon_step = spacing_km / (
                111.195 * max(0.05, math.cos(math.radians(latitude)))
            )
            columns = max(1, math.ceil((east - west) / lon_step))
            for column in range(columns):
                cell_west = west + column * (east - west) / columns
                cell_east = west + (column + 1) * (east - west) / columns
                if polygon.intersects(box(cell_west, cell_south, cell_east, cell_north)):
                    centers.add(
                        (round(latitude, 6), round((cell_west + cell_east) / 2, 6))
                    )
    return [
        {
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": radius_km,
            "countries": [country],
        }
        for latitude, longitude in sorted(centers)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--radius-km", type=int, default=40)
    parser.add_argument("countries", nargs="+", choices=sorted(ISO3))
    args = parser.parse_args()

    existing = json.loads(args.input.read_text(encoding="utf-8"))
    retained = [
        area for area in existing if not set(area["countries"]).intersection(args.countries)
    ]
    generated: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    with httpx.Client(timeout=httpx.Timeout(30), follow_redirects=True) as client:
        for country in args.countries:
            boundary, sources[country] = load_boundary(country, client)
            country_discs = generate_discs(boundary, country, args.radius_km)
            generated.extend(country_discs)
            print(f"{country}: {len(country_discs)} discs", flush=True)

    args.output.write_text(
        json.dumps(retained + generated, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"areas": len(retained + generated), "boundary_sources": sources}, sort_keys=True))


if __name__ == "__main__":
    main()
