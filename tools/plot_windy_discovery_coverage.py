"""Render configured Windy discovery discs as a PNG coverage overview."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


CANVAS_SIZE = (1600, 900)
BACKGROUND = (246, 248, 251, 255)
GRID = (190, 199, 211, 255)
TEXT = (24, 34, 48, 255)
DISC_FILL = (27, 124, 219, 38)
DISC_EDGE = (11, 91, 168, 150)
CENTER = (208, 48, 80, 230)


def read_areas(env_file: Path) -> list[dict[str, Any]]:
    """Read the Windy discovery areas file referenced by the env file."""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name == "WINDY_DISCOVERY_AREAS_FILE":
            areas_file = Path(value)
            if not areas_file.is_absolute():
                areas_file = env_file.parent / areas_file
            areas = json.loads(areas_file.read_text(encoding="utf-8"))
            if not isinstance(areas, list) or not areas:
                raise ValueError("Windy discovery area list is empty")
            return areas
    raise ValueError(f"WINDY_DISCOVERY_AREAS_FILE is absent from {env_file}")


def render(areas: list[dict[str, Any]], output: Path) -> None:
    image = Image.new("RGBA", CANVAS_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default(size=20)
    title_font = ImageFont.load_default(size=32)
    panels = {
        "FI": (50, 110, 770, 840),
        "FR": (830, 110, 1550, 840),
    }

    draw.text(
        (50, 35),
        "Windy discovery coverage - 50 km query discs",
        fill=TEXT,
        font=title_font,
    )
    for country, box in panels.items():
        selected = [area for area in areas if country in area["countries"]]
        if not selected:
            continue
        _render_panel(draw, selected, country, box, font, title_font)
    image.convert("RGB").save(output, "PNG", optimize=True)


def _render_panel(
    draw: ImageDraw.ImageDraw,
    areas: list[dict[str, Any]],
    country: str,
    box: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
    title_font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = box
    latitudes = [float(area["latitude"]) for area in areas]
    longitudes = [float(area["longitude"]) for area in areas]
    mean_lat = sum(latitudes) / len(latitudes)
    radius_padding = max(float(area["radius_km"]) for area in areas) / 111.195
    min_lat, max_lat = min(latitudes) - radius_padding, max(latitudes) + radius_padding
    lon_padding = radius_padding / math.cos(math.radians(mean_lat))
    min_lon, max_lon = min(longitudes) - lon_padding, max(longitudes) + lon_padding

    draw.rounded_rectangle(box, radius=18, fill=(255, 255, 255, 255), outline=GRID, width=2)
    label = "Finland" if country == "FI" else "Metropolitan France and Corsica"
    draw.text((left + 22, top + 16), label, fill=TEXT, font=title_font)
    draw.text(
        (left + 22, top + 55),
        f"{len(areas)} configured discs",
        fill=TEXT,
        font=font,
    )

    plot = (left + 50, top + 95, right - 30, bottom - 45)
    px0, py0, px1, py1 = plot

    def project(latitude: float, longitude: float) -> tuple[float, float]:
        x = px0 + (longitude - min_lon) / (max_lon - min_lon) * (px1 - px0)
        y = py1 - (latitude - min_lat) / (max_lat - min_lat) * (py1 - py0)
        return x, y

    for latitude in _ticks(min_lat, max_lat):
        _, y = project(latitude, min_lon)
        draw.line((px0, y, px1, y), fill=GRID, width=1)
        draw.text((left + 5, y - 8), f"{latitude:.0f}°", fill=TEXT, font=font)
    for longitude in _ticks(min_lon, max_lon):
        x, _ = project(min_lat, longitude)
        draw.line((x, py0, x, py1), fill=GRID, width=1)
        draw.text((x - 14, py1 + 8), f"{longitude:.0f}°", fill=TEXT, font=font)

    for area in areas:
        latitude = float(area["latitude"])
        longitude = float(area["longitude"])
        radius_km = float(area["radius_km"])
        x, y = project(latitude, longitude)
        north_x, north_y = project(latitude + radius_km / 111.195, longitude)
        east_x, _ = project(
            latitude,
            longitude + radius_km / (111.195 * math.cos(math.radians(latitude))),
        )
        rx, ry = abs(east_x - x), abs(north_y - y)
        draw.ellipse((x - rx, y - ry, x + rx, y + ry), fill=DISC_FILL, outline=DISC_EDGE)
        draw.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=CENTER)


def _ticks(low: float, high: float) -> list[float]:
    span = high - low
    step = 1 if span <= 8 else 2
    first = math.ceil(low / step) * step
    return [float(value) for value in range(int(first), math.floor(high) + 1, step)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.example"))
    parser.add_argument(
        "--output", type=Path, default=Path("windy_discovery_coverage.png")
    )
    args = parser.parse_args()
    render(read_areas(args.env_file), args.output)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
