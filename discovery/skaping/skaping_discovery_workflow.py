"""Discover Skaping image points of view and reconcile the shared registry."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
import math
import time
from typing import Any, Mapping

import psycopg

from config.deployment_config import (
    AltitudeConfig,
    DatabaseConfig,
    SkapingConfig,
)
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
    compact_identifier,
    validate_internal_identifier,
)
from discovery.skaping.skaping_source_access import (
    SkapingClient,
    SkapingDiscoveryError,
)


NETWORK_ID = "ska"


class SkapingIdentifierError(SkapingDiscoveryError, IdentifierEstablishmentError):
    """Skaping data cannot establish a valid compact internal identifier."""


@dataclass(frozen=True)
class SkapingCamera:
    provider_id: str
    latitude: float
    longitude: float
    altitude: float | None
    country: str | None
    metadata: dict[str, Any]
    image_points_of_view: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SkapingDiscoverySnapshot:
    sites: tuple[DiscoveredSite, ...]
    source_streams: tuple[DiscoveredSourceStream, ...]
    cameras_seen: int
    cameras_accepted: int
    cameras_excluded_country: int
    points_of_view_seen: int
    image_points_of_view_accepted: int
    non_image_points_of_view_excluded: int


def build_discovery_snapshot(
    cameras_payload: list[Mapping[str, Any]],
    stored: RegistrySnapshot,
    *,
    member_countries: tuple[str, ...],
    selected_rendition: str = "mini",
) -> SkapingDiscoverySnapshot:
    cameras: list[SkapingCamera] = []
    excluded_country = points_seen = excluded_media = 0
    seen_camera_ids: set[str] = set()
    for raw_camera in cameras_payload:
        camera, camera_points_seen, camera_points_excluded = _normalize_camera(
            raw_camera,
            member_countries=member_countries,
        )
        points_seen += camera_points_seen
        excluded_media += camera_points_excluded
        if camera is None:
            excluded_country += 1
            continue
        if camera.provider_id in seen_camera_ids:
            raise SkapingIdentifierError(
                f"duplicate Skaping camera: {camera.provider_id}"
            )
        seen_camera_ids.add(camera.provider_id)
        cameras.append(camera)

    existing_sites = {
        str(row["provider_site_id"]): row
        for row in stored.sites.values()
        if row.get("provider_site_id") is not None
    }
    existing_streams = {
        (str(row["site_id"]), str(row["provider_source_stream_id"])): row
        for row in stored.source_streams.values()
    }
    used_site_ids = set(stored.sites)
    used_stream_ids = set(stored.source_streams)
    sites: list[DiscoveredSite] = []
    streams: list[DiscoveredSourceStream] = []

    for camera in sorted(cameras, key=lambda item: item.provider_id):
        previous_site = existing_sites.get(camera.provider_id)
        site_id = (
            validate_internal_identifier(
                str(previous_site["site_id"]), error_type=SkapingIdentifierError
            )
            if previous_site is not None
            else compact_identifier(
                "ska",
                camera.provider_id,
                used_site_ids,
                error_type=SkapingIdentifierError,
            )
        )
        used_site_ids.add(site_id)
        coordinates_unchanged = (
            previous_site is not None
            and float(previous_site["latitude"]) == camera.latitude
            and float(previous_site["longitude"]) == camera.longitude
        )
        altitude = camera.altitude
        if (
            altitude is None
            and coordinates_unchanged
            and previous_site.get("altitude") is not None
        ):
            altitude = float(previous_site["altitude"])
        country = camera.country
        if (
            country is None
            and coordinates_unchanged
            and previous_site.get("country") is not None
        ):
            country = str(previous_site["country"])
        sites.append(
            DiscoveredSite(
                site_id=site_id,
                provider_site_id=camera.provider_id,
                latitude=camera.latitude,
                longitude=camera.longitude,
                altitude=altitude,
                country=country,
                provider_metadata=camera.metadata,
            )
        )

        seen_pov_ids: set[str] = set()
        for point_of_view in camera.image_points_of_view:
            pov_id = str(point_of_view["id"])
            if pov_id in seen_pov_ids:
                raise SkapingIdentifierError(
                    f"duplicate point of view {pov_id} in camera {camera.provider_id}"
                )
            seen_pov_ids.add(pov_id)
            previous_stream = existing_streams.get((site_id, pov_id))
            stream_id = (
                validate_internal_identifier(
                    str(previous_stream["source_stream_id"]),
                    error_type=SkapingIdentifierError,
                )
                if previous_stream is not None
                else compact_identifier(
                    f"{site_id}POV",
                    pov_id,
                    used_stream_ids,
                    error_type=SkapingIdentifierError,
                )
            )
            used_stream_ids.add(stream_id)
            streams.append(
                DiscoveredSourceStream(
                    source_stream_id=stream_id,
                    site_id=site_id,
                    provider_source_stream_id=pov_id,
                    selected_rendition=selected_rendition,
                    provider_metadata=point_of_view,
                )
            )

    return SkapingDiscoverySnapshot(
        sites=tuple(sites),
        source_streams=tuple(streams),
        cameras_seen=len(cameras_payload),
        cameras_accepted=len(cameras),
        cameras_excluded_country=excluded_country,
        points_of_view_seen=points_seen,
        image_points_of_view_accepted=len(streams),
        non_image_points_of_view_excluded=excluded_media,
    )


def _normalize_camera(
    raw_camera: Mapping[str, Any],
    *,
    member_countries: tuple[str, ...],
) -> tuple[SkapingCamera | None, int, int]:
    provider_id = _required_identifier(raw_camera.get("id"), "camera")
    latitude = _coordinate(raw_camera.get("latitude"), "latitude", provider_id)
    longitude = _coordinate(raw_camera.get("longitude"), "longitude", provider_id)
    altitude = _optional_altitude(raw_camera.get("altitude"), provider_id)
    points = raw_camera.get("point_of_views")
    if not isinstance(points, list):
        raise SkapingDiscoveryError(
            f"Skaping camera {provider_id} has no point_of_views array"
        )
    normalized_points: list[dict[str, Any]] = []
    for point in points:
        if not isinstance(point, Mapping):
            raise SkapingDiscoveryError(
                f"Skaping camera {provider_id} has a malformed point of view"
            )
        _required_identifier(point.get("id"), "point of view")
        media_type = point.get("type")
        if not isinstance(media_type, str) or not media_type.strip():
            raise SkapingDiscoveryError(
                f"Skaping point of view {point.get('id')} has invalid type"
            )
        if media_type.strip().lower() == "image":
            normalized_points.append(dict(point))
    country = _country_code(raw_camera)
    if country is not None and country not in member_countries:
        return None, len(points), len(points) - len(normalized_points)
    return (
        SkapingCamera(
            provider_id=provider_id,
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            country=country,
            metadata=dict(raw_camera),
            image_points_of_view=tuple(normalized_points),
        ),
        len(points),
        len(points) - len(normalized_points),
    )


def _required_identifier(value: Any, entity: str) -> str:
    if (
        not isinstance(value, (str, int))
        or isinstance(value, bool)
        or not str(value).strip()
    ):
        raise SkapingIdentifierError(
            f"Skaping {entity} has an invalid identifier"
        )
    return str(value).strip()


def _coordinate(value: Any, name: str, camera_id: str) -> float:
    try:
        coordinate = float(value)
    except (TypeError, ValueError) as error:
        raise SkapingDiscoveryError(
            f"Skaping camera {camera_id} has invalid {name}"
        ) from error
    limit = 90 if name == "latitude" else 180
    if not math.isfinite(coordinate) or not -limit <= coordinate <= limit:
        raise SkapingDiscoveryError(
            f"Skaping camera {camera_id} has invalid {name}"
        )
    return coordinate


def _optional_altitude(value: Any, camera_id: str) -> float | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "null"}):
        return None
    try:
        altitude = float(value)
    except (TypeError, ValueError) as error:
        raise SkapingDiscoveryError(
            f"Skaping camera {camera_id} has invalid altitude"
        ) from error
    if not math.isfinite(altitude):
        raise SkapingDiscoveryError(
            f"Skaping camera {camera_id} has invalid altitude"
        )
    return altitude


def _country_code(camera: Mapping[str, Any]) -> str | None:
    for key in ("country_code", "countryCode", "country"):
        value = camera.get(key)
        if isinstance(value, str):
            candidate = value.strip().upper()
            if len(candidate) == 2 and candidate.isalpha():
                return candidate
    return None


def run_discovery(
    *,
    dry_run: bool = False,
    metrics: DiscoveryMetrics | None = None,
) -> tuple[
    SkapingDiscoverySnapshot,
    DiscoveryUpdateResult | None,
    dict[str, int],
]:
    skaping = SkapingConfig.from_environment()
    database = DatabaseConfig.from_environment()
    with SkapingClient(
        skaping.read_api_key(),
        summary_url=skaping.summary_url,
        timeout_s=skaping.request_timeout_s,
        retry_count=skaping.retry_count,
        retry_backoff_s=skaping.retry_backoff_s,
        minimum_camera_count=skaping.minimum_camera_count,
        request_delay_s=skaping.request_delay_s,
        request_observer=(
            metrics.observe_provider_request if metrics is not None else None
        ),
    ) as client:
        payload = client.fetch_cameras()
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
            payload,
            stored,
            member_countries=skaping.member_countries,
            selected_rendition=skaping.selected_rendition,
        )
        if dry_run:
            return snapshot, None, registry_status_counts(stored.source_streams)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover and register Skaping image points of view"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="retrieve and compare without writing",
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
        sources_seen=snapshot.image_points_of_view_accepted,
        sources_added=update.streams_inserted if update is not None else 0,
        sources_updated=update.streams_updated if update is not None else 0,
        sources_disabled=update.streams_inactivated if update is not None else 0,
        status_counts=status_counts,
    )
    output: dict[str, object] = {
        "dry_run": args.dry_run,
        "cameras_seen": snapshot.cameras_seen,
        "cameras_accepted": snapshot.cameras_accepted,
        "cameras_excluded_country": snapshot.cameras_excluded_country,
        "points_of_view_seen": snapshot.points_of_view_seen,
        "image_points_of_view_accepted": snapshot.image_points_of_view_accepted,
        "non_image_points_of_view_excluded": (
            snapshot.non_image_points_of_view_excluded
        ),
        "sites": len(snapshot.sites),
        "source_streams": len(snapshot.source_streams),
        "duration_seconds": round(duration_s, 6),
        "discovery_metrics_published": metrics_published,
    }
    if update is not None:
        output["registry_update"] = asdict(update)
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
