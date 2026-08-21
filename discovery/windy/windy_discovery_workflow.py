"""Discover Windy webcams and reconcile them with the shared registry."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
import math
import time
from typing import Any, Mapping, Sequence

import psycopg

from config.deployment_config import AltitudeConfig, DatabaseConfig, WindyConfig
from database.registry_queries import (
    DiscoveredSite,
    DiscoveredSourceStream,
    DiscoveryUpdateResult,
    RegistrySnapshot,
    apply_discovery_update,
    get_network_registry,
)
from discovery.shared.add_altitude import enrich_missing_altitudes
from discovery.shared.altitude_lookup import AltitudeClient
from discovery.shared.discovery_metrics import (
    DiscoveryMetrics,
    registry_status_counts,
)
from discovery.shared.identifiers import (
    IdentifierEstablishmentError,
    establish_identifier,
    validate_internal_identifier,
)
from discovery.windy.windy_source_access import WindyClient, WindyDiscoveryError


NETWORK_ID = "win"


class WindyIdentifierError(WindyDiscoveryError, IdentifierEstablishmentError):
    """Windy data cannot establish a valid compact internal identifier."""


EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class WindyWebcam:
    provider_id: str
    title: str
    latitude: float
    longitude: float
    country: str
    city: str | None
    categories: tuple[str, ...]
    provider_metadata: dict[str, Any]


@dataclass(frozen=True)
class WindyDiscoverySnapshot:
    sites: tuple[DiscoveredSite, ...]
    source_streams: tuple[DiscoveredSourceStream, ...]
    excluded_count: int


def normalize_webcam(raw: Mapping[str, Any], allowed_countries: set[str]) -> WindyWebcam | None:
    """Validate and normalize a Windy record; return None for out-of-scope records."""
    provider_id = raw.get("webcamId")
    title = raw.get("title")
    location = raw.get("location")
    if not isinstance(provider_id, (str, int)) or isinstance(provider_id, bool):
        raise WindyIdentifierError("Windy webcam has an invalid webcamId")
    if not isinstance(title, str) or not title.strip():
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has no title")
    if not isinstance(location, Mapping):
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has no location")
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError) as error:
        raise WindyDiscoveryError(
            f"Windy webcam {provider_id} has invalid coordinates"
        ) from error
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has invalid latitude")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has invalid longitude")

    country = location.get("countryCode", location.get("country_code"))
    if not isinstance(country, str) or len(country.strip()) != 2:
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has no country code")
    country = country.upper()
    categories = _normalize_categories(raw.get("categories", []), str(provider_id))
    provider_status = raw.get("status", "active")
    if country not in allowed_countries or "indoor" in categories:
        return None

    city = location.get("city")
    city = city.strip() if isinstance(city, str) and city.strip() else None
    metadata = {
        "webcamId": str(provider_id),
        "title": title.strip(),
        "status": provider_status,
        "categories": list(categories),
        "location": {
            "city": city,
            "countryCode": country,
            "latitude": latitude,
            "longitude": longitude,
        },
    }
    return WindyWebcam(
        provider_id=str(provider_id),
        title=title.strip(),
        latitude=latitude,
        longitude=longitude,
        country=country,
        city=city,
        categories=categories,
        provider_metadata=metadata,
    )


def build_discovery_snapshot(
    raw_webcams: Sequence[Mapping[str, Any]],
    stored: RegistrySnapshot,
    *,
    allowed_countries: set[str],
    site_distance_threshold_m: float,
    selected_rendition: str = "preview",
) -> WindyDiscoverySnapshot:
    normalized: list[WindyWebcam] = []
    excluded = 0
    for raw in raw_webcams:
        webcam = normalize_webcam(raw, allowed_countries)
        if webcam is None:
            excluded += 1
        else:
            normalized.append(webcam)
    if len({item.provider_id for item in normalized}) != len(normalized):
        raise WindyIdentifierError(
            "normalized Windy snapshot contains duplicate webcams"
        )

    existing_streams = {
        str(row["provider_source_stream_id"]): row
        for row in stored.source_streams.values()
    }
    site_rows: dict[str, dict[str, Any]] = {
        site_id: dict(row) for site_id, row in stored.sites.items()
    }
    assigned_site_ids: dict[str, str] = {}
    used_stream_ids = set(stored.source_streams)

    for webcam in sorted(normalized, key=lambda item: item.provider_id):
        existing = existing_streams.get(webcam.provider_id)
        if existing is not None:
            assigned_site_ids[webcam.provider_id] = validate_internal_identifier(
                str(existing["site_id"]), error_type=WindyIdentifierError
            )
            continue
        nearest_site_id, nearest_distance = _nearest_site(webcam, site_rows)
        if nearest_site_id is not None and nearest_distance <= site_distance_threshold_m:
            assigned_site_ids[webcam.provider_id] = nearest_site_id
            continue
        site_id = _new_identifier("win", webcam.provider_id, site_rows)
        assigned_site_ids[webcam.provider_id] = site_id
        site_rows[site_id] = {
            "site_id": site_id,
            "provider_site_id": webcam.provider_id,
            "latitude": webcam.latitude,
            "longitude": webcam.longitude,
            "altitude": None,
            "country": webcam.country,
            "provider_metadata": webcam.provider_metadata,
        }

    normalized_by_id = {webcam.provider_id: webcam for webcam in normalized}
    referenced_site_ids = set(assigned_site_ids.values())
    sites: list[DiscoveredSite] = []
    for site_id in sorted(referenced_site_ids):
        row = site_rows[site_id]
        anchor = normalized_by_id.get(str(row.get("provider_site_id")))
        if anchor is not None:
            sites.append(_site_from_webcam(site_id, anchor, previous=row))
        else:
            sites.append(_site_from_stored(row))

    streams: list[DiscoveredSourceStream] = []
    for webcam in sorted(normalized, key=lambda item: item.provider_id):
        existing = existing_streams.get(webcam.provider_id)
        stream_id = (
            validate_internal_identifier(
                str(existing["source_stream_id"]), error_type=WindyIdentifierError
            )
            if existing is not None
            else establish_identifier(
                "win",
                webcam.provider_id,
                used_stream_ids,
                error_type=WindyIdentifierError,
            )
        )
        used_stream_ids.add(stream_id)
        streams.append(
            DiscoveredSourceStream(
                source_stream_id=stream_id,
                site_id=assigned_site_ids[webcam.provider_id],
                provider_source_stream_id=webcam.provider_id,
                selected_rendition=selected_rendition,
                provider_metadata=webcam.provider_metadata,
            )
        )
    return WindyDiscoverySnapshot(tuple(sites), tuple(streams), excluded)


def run_discovery(
    *,
    dry_run: bool = False,
    metrics: DiscoveryMetrics | None = None,
) -> tuple[
    WindyDiscoverySnapshot,
    DiscoveryUpdateResult | None,
    dict[str, int],
]:
    windy = WindyConfig.from_environment()
    database = DatabaseConfig.from_environment()
    allowed_countries = set(windy.member_countries)
    with WindyClient(
        windy.read_api_key(),
        timeout_s=windy.request_timeout_s,
        page_size=windy.page_size,
        request_delay_s=windy.request_delay_s,
        cache_file=windy.discovery_cache_file,
        request_observer=(
            metrics.observe_provider_request if metrics is not None else None
        ),
    ) as client:
        raw_webcams = client.discover_members(
            windy.member_countries, windy.discovery_areas
        )
    with psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.name,
        user=database.user,
        password=database.read_password(),
        connect_timeout=5,
    ) as connection:
        stored = get_network_registry(connection, NETWORK_ID)
        snapshot = build_discovery_snapshot(
            raw_webcams,
            stored,
            allowed_countries=allowed_countries,
            site_distance_threshold_m=windy.site_distance_threshold_m,
            selected_rendition=windy.selected_rendition,
        )
        if dry_run:
            return (
                snapshot,
                None,
                registry_status_counts(stored.source_streams),
            )
        update = apply_discovery_update(
            connection, NETWORK_ID, snapshot.sites, snapshot.source_streams
        )
        altitude = AltitudeConfig.from_environment()
        if altitude.enabled:
            with AltitudeClient(
                altitude.provider_url,
                timeout_s=altitude.request_timeout_s,
                request_delay_s=altitude.request_delay_s,
                max_attempts=altitude.max_attempts,
            ) as altitude_client:
                enrichment = enrich_missing_altitudes(
                    connection,
                    NETWORK_ID,
                    altitude_client,
                    limit=altitude.max_sites_per_run,
                    batch_size=altitude.batch_size,
                )
            update = replace(
                update,
                altitudes_eligible=enrichment.eligible,
                altitudes_resolved=enrichment.resolved,
                altitudes_unresolved=enrichment.unresolved,
                altitudes_updated=enrichment.updated,
            )
        refreshed = get_network_registry(connection, NETWORK_ID)
        return (
            snapshot,
            update,
            registry_status_counts(refreshed.source_streams),
        )


def _normalize_categories(value: Any, provider_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise WindyDiscoveryError(f"Windy webcam {provider_id} has invalid categories")
    categories: set[str] = set()
    for category in value:
        if isinstance(category, str):
            category_id = category
        elif isinstance(category, Mapping) and isinstance(category.get("id"), str):
            category_id = category["id"]
        else:
            raise WindyDiscoveryError(
                f"Windy webcam {provider_id} has a malformed category"
            )
        categories.update(part.lower() for part in category_id.split("_") if part)
    return tuple(sorted(categories))


def _nearest_site(
    webcam: WindyWebcam, site_rows: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, float]:
    nearest_id: str | None = None
    nearest_distance = math.inf
    for site_id, site in site_rows.items():
        distance = _haversine_m(
            webcam.latitude,
            webcam.longitude,
            float(site["latitude"]),
            float(site["longitude"]),
        )
        if (distance, site_id) < (nearest_distance, nearest_id or ""):
            nearest_id, nearest_distance = site_id, distance
    return nearest_id, nearest_distance


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + (
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(value))


def _new_identifier(
    prefix: str,
    provider_id: str,
    used: Mapping[str, Any] | set[str],
) -> str:
    return establish_identifier(
        prefix, provider_id, used, error_type=WindyIdentifierError
    )


def _site_from_webcam(
    site_id: str,
    webcam: WindyWebcam,
    *,
    previous: Mapping[str, Any] | None = None,
) -> DiscoveredSite:
    altitude = None
    if (
        previous is not None
        and float(previous["latitude"]) == webcam.latitude
        and float(previous["longitude"]) == webcam.longitude
        and previous.get("altitude") is not None
    ):
        altitude = float(previous["altitude"])
    return DiscoveredSite(
        site_id=site_id,
        provider_site_id=webcam.provider_id,
        latitude=webcam.latitude,
        longitude=webcam.longitude,
        altitude=altitude,
        country=webcam.country,
        provider_metadata=webcam.provider_metadata,
    )


def _site_from_stored(row: Mapping[str, Any]) -> DiscoveredSite:
    return DiscoveredSite(
        site_id=row["site_id"],
        provider_site_id=row.get("provider_site_id"),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        altitude=float(row["altitude"]) if row.get("altitude") is not None else None,
        country=row.get("country"),
        provider_metadata=row.get("provider_metadata") or {},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and register Windy webcams")
    parser.add_argument(
        "--dry-run", action="store_true", help="retrieve and compare without writing"
    )
    args = parser.parse_args()
    metrics = DiscoveryMetrics.from_environment(NETWORK_ID)
    started = time.monotonic()
    try:
        snapshot, update, status_counts = run_discovery(
            dry_run=args.dry_run,
            metrics=metrics,
        )
    except Exception as error:
        if isinstance(error, IdentifierEstablishmentError):
            metrics.observe_identifier_violation()
        metrics.publish_failure(duration_s=time.monotonic() - started)
        raise
    duration_s = time.monotonic() - started
    metrics_published = metrics.publish_success(
        duration_s=duration_s,
        sources_seen=len(snapshot.source_streams),
        sources_added=update.streams_inserted if update is not None else 0,
        sources_updated=update.streams_updated if update is not None else 0,
        sources_disabled=update.streams_inactivated if update is not None else 0,
        status_counts=status_counts,
    )
    output = {
        "dry_run": args.dry_run,
        "sites": len(snapshot.sites),
        "source_streams": len(snapshot.source_streams),
        "excluded": snapshot.excluded_count,
        "duration_seconds": round(duration_s, 6),
        "discovery_metrics_published": metrics_published,
    }
    if update is not None:
        output.update(asdict(update))
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
