"""Discover Fintraffic weather cameras and reconcile the shared registry."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
import math
import time
from typing import Any, Mapping, Sequence

import psycopg

from config.deployment_config import AltitudeConfig, DatabaseConfig, FintrafficConfig
from database.registry_queries import (
    DiscoveredSite,
    DiscoveredSourceStream,
    DiscoveryUpdateResult,
    RegistrySnapshot,
    apply_discovery_update,
    get_network_registry,
)
from discovery.fintraffic.fintraffic_source_access import (
    FintrafficClient,
    FintrafficDiscoveryError,
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


NETWORK_ID = "fin"


class FintrafficIdentifierError(
    FintrafficDiscoveryError, IdentifierEstablishmentError
):
    """Fintraffic data cannot establish a valid compact internal identifier."""


@dataclass(frozen=True)
class FintrafficStation:
    provider_id: str
    latitude: float
    longitude: float
    metadata: dict[str, Any]
    presets: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FintrafficDiscoverySnapshot:
    sites: tuple[DiscoveredSite, ...]
    source_streams: tuple[DiscoveredSourceStream, ...]
    provider_stations_seen: int
    stations_accepted: int
    stations_excluded: int
    presets_seen: int
    presets_accepted: int
    presets_excluded: int


def build_discovery_snapshot(
    payload: Mapping[str, Any],
    stored: RegistrySnapshot,
    *,
    selected_rendition: str = "full_jpeg",
    selected_collection_status: str = "GATHERING",
    require_in_collection: bool = True,
) -> FintrafficDiscoverySnapshot:
    features = payload.get("features")
    if not isinstance(features, list):
        raise FintrafficDiscoveryError("Fintraffic snapshot has no features array")

    stations: list[FintrafficStation] = []
    stations_excluded = presets_seen = presets_excluded = 0
    seen_station_ids: set[str] = set()
    for feature in features:
        if not isinstance(feature, Mapping):
            raise FintrafficDiscoveryError("Fintraffic snapshot has a malformed feature")
        station, feature_presets_seen, feature_presets_excluded = _normalize_station(
            feature,
            selected_collection_status=selected_collection_status,
            require_in_collection=require_in_collection,
        )
        presets_seen += feature_presets_seen
        presets_excluded += feature_presets_excluded
        if station is None:
            stations_excluded += 1
            continue
        if station.provider_id in seen_station_ids:
            raise FintrafficIdentifierError(
                f"duplicate Fintraffic station: {station.provider_id}"
            )
        seen_station_ids.add(station.provider_id)
        stations.append(station)

    existing_sites = {
        str(row["provider_site_id"]): row
        for row in stored.sites.values()
        if row.get("provider_site_id") is not None
    }
    existing_streams = {
        str(row["provider_source_stream_id"]): row
        for row in stored.source_streams.values()
    }
    used_site_ids = set(stored.sites)
    used_stream_ids = set(stored.source_streams)
    sites: list[DiscoveredSite] = []
    streams: list[DiscoveredSourceStream] = []

    seen_preset_ids: set[str] = set()
    for station in sorted(stations, key=lambda item: item.provider_id):
        previous_site = existing_sites.get(station.provider_id)
        site_id = (
            validate_internal_identifier(
                str(previous_site["site_id"]), error_type=FintrafficIdentifierError
            )
            if previous_site is not None
            else compact_identifier(
                "fin",
                station.provider_id,
                used_site_ids,
                error_type=FintrafficIdentifierError,
            )
        )
        used_site_ids.add(site_id)
        altitude = None
        if (
            previous_site is not None
            and float(previous_site["latitude"]) == station.latitude
            and float(previous_site["longitude"]) == station.longitude
            and previous_site.get("altitude") is not None
        ):
            altitude = float(previous_site["altitude"])
        sites.append(
            DiscoveredSite(
                site_id=site_id,
                provider_site_id=station.provider_id,
                latitude=station.latitude,
                longitude=station.longitude,
                altitude=altitude,
                country="FI",
                provider_metadata=station.metadata,
            )
        )

        for preset in station.presets:
            preset_id = str(preset["id"])
            if not preset_id.startswith(station.provider_id):
                raise FintrafficIdentifierError(
                    f"Fintraffic preset {preset_id} does not start with station "
                    f"identifier {station.provider_id}"
                )
            if preset_id in seen_preset_ids:
                raise FintrafficIdentifierError(
                    f"duplicate preset identifier: {preset_id}"
                )
            seen_preset_ids.add(preset_id)
            previous_stream = existing_streams.get(preset_id)
            if (
                previous_stream is not None
                and previous_stream["site_id"] != site_id
            ):
                raise FintrafficIdentifierError(
                    f"preset {preset_id} moved between Fintraffic stations"
                )
            stream_id = (
                validate_internal_identifier(
                    str(previous_stream["source_stream_id"]),
                    error_type=FintrafficIdentifierError,
                )
                if previous_stream is not None
                else compact_identifier(
                    "fin",
                    preset_id,
                    used_stream_ids,
                    error_type=FintrafficIdentifierError,
                )
            )
            used_stream_ids.add(stream_id)
            streams.append(
                DiscoveredSourceStream(
                    source_stream_id=stream_id,
                    site_id=site_id,
                    provider_source_stream_id=preset_id,
                    selected_rendition=selected_rendition,
                    provider_metadata=preset,
                )
            )

    return FintrafficDiscoverySnapshot(
        sites=tuple(sites),
        source_streams=tuple(streams),
        provider_stations_seen=len(features),
        stations_accepted=len(stations),
        stations_excluded=stations_excluded,
        presets_seen=presets_seen,
        presets_accepted=len(streams),
        presets_excluded=presets_excluded,
    )


def _normalize_station(
    feature: Mapping[str, Any],
    *,
    selected_collection_status: str,
    require_in_collection: bool,
) -> tuple[FintrafficStation | None, int, int]:
    provider_id = _required_identifier(feature.get("id"), "station")
    geometry = feature.get("geometry")
    properties = feature.get("properties")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid geometry"
        )
    coordinates = geometry.get("coordinates")
    if (
        not isinstance(coordinates, Sequence)
        or isinstance(coordinates, (str, bytes))
        or len(coordinates) < 2
    ):
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid coordinates"
        )
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError) as error:
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid coordinates"
        ) from error
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid longitude"
        )
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid latitude"
        )
    if not isinstance(properties, Mapping):
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has invalid properties"
        )
    property_id = properties.get("id")
    if property_id is not None and str(property_id) != provider_id:
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has inconsistent identifiers"
        )
    presets = properties.get("presets")
    if not isinstance(presets, list):
        raise FintrafficDiscoveryError(
            f"Fintraffic station {provider_id} has no presets array"
        )
    for preset in presets:
        if not isinstance(preset, Mapping):
            raise FintrafficDiscoveryError(
                f"Fintraffic station {provider_id} has a malformed preset"
            )
        _required_identifier(preset.get("id"), "preset")
        if not isinstance(preset.get("inCollection"), bool):
            raise FintrafficDiscoveryError(
                f"Fintraffic preset {preset.get('id')} has invalid inCollection"
            )
    if properties.get("collectionStatus") != selected_collection_status:
        return None, len(presets), len(presets)
    accepted = tuple(
        dict(preset)
        for preset in presets
        if not require_in_collection or preset["inCollection"] is True
    )
    return (
        FintrafficStation(
            provider_id=provider_id,
            latitude=latitude,
            longitude=longitude,
            metadata=dict(feature),
            presets=accepted,
        ),
        len(presets),
        len(presets) - len(accepted),
    )


def _required_identifier(value: Any, entity: str) -> str:
    if (
        not isinstance(value, (str, int))
        or isinstance(value, bool)
        or not str(value).strip()
    ):
        raise FintrafficIdentifierError(
            f"Fintraffic {entity} has an invalid identifier"
        )
    return str(value).strip()


def run_discovery(
    *,
    dry_run: bool = False,
    metrics: DiscoveryMetrics | None = None,
) -> tuple[
    FintrafficDiscoverySnapshot,
    DiscoveryUpdateResult | None,
    dict[str, int],
]:
    fintraffic = FintrafficConfig.from_environment()
    database = DatabaseConfig.from_environment()
    with FintrafficClient(
        fintraffic.user_header,
        stations_url=fintraffic.stations_url,
        timeout_s=fintraffic.request_timeout_s,
        retry_count=fintraffic.retry_count,
        retry_backoff_s=fintraffic.retry_backoff_s,
        request_delay_s=fintraffic.request_delay_s,
        request_observer=(
            metrics.observe_provider_request if metrics is not None else None
        ),
    ) as client:
        payload = client.fetch_stations()
        payload = _expand_station_details(
            payload,
            client,
            selected_collection_status=fintraffic.selected_collection_status,
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
            payload,
            stored,
            selected_rendition=fintraffic.selected_rendition,
            selected_collection_status=fintraffic.selected_collection_status,
            require_in_collection=fintraffic.require_in_collection,
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


def _expand_station_details(
    payload: Mapping[str, Any],
    client: FintrafficClient,
    *,
    selected_collection_status: str,
) -> dict[str, Any]:
    """Replace eligible compact features with complete station details."""
    features = payload.get("features")
    if not isinstance(features, list):
        raise FintrafficDiscoveryError("Fintraffic snapshot has no features array")
    expanded: list[Mapping[str, Any]] = []
    for feature in features:
        if not isinstance(feature, Mapping):
            raise FintrafficDiscoveryError(
                "Fintraffic snapshot has a malformed feature"
            )
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            raise FintrafficDiscoveryError(
                "Fintraffic station has invalid properties"
            )
        if properties.get("collectionStatus") == selected_collection_status:
            station_id = _required_identifier(feature.get("id"), "station")
            expanded.append(client.fetch_station(station_id))
        else:
            expanded.append(feature)
    return {**dict(payload), "features": expanded}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover and register Fintraffic weather cameras"
    )
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
        sources_seen=snapshot.presets_accepted,
        sources_added=update.streams_inserted if update is not None else 0,
        sources_updated=update.streams_updated if update is not None else 0,
        sources_disabled=update.streams_inactivated if update is not None else 0,
        status_counts=status_counts,
    )
    output = {
        "dry_run": args.dry_run,
        "provider_stations_seen": snapshot.provider_stations_seen,
        "stations_accepted": snapshot.stations_accepted,
        "stations_excluded": snapshot.stations_excluded,
        "presets_seen": snapshot.presets_seen,
        "presets_accepted": snapshot.presets_accepted,
        "presets_excluded": snapshot.presets_excluded,
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
