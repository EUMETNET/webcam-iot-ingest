"""Provider-independent access to the webcam registry and ingestion state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


StreamStatus = Literal["active", "inactive", "blacklisted"]
JsonObject = Mapping[str, Any]


class RegistryError(RuntimeError):
    """Base class for registry access failures."""


class RegistryCollisionError(RegistryError):
    """An internal identifier is already assigned to another provider entity."""


class RegistryRecordNotFoundError(RegistryError):
    """A requested registry record does not exist."""


@dataclass(frozen=True)
class DiscoveredSite:
    site_id: str
    provider_site_id: str | None
    latitude: float
    longitude: float
    altitude: float | None = None
    country: str | None = None
    provider_metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveredSourceStream:
    source_stream_id: str
    site_id: str
    provider_source_stream_id: str
    selected_rendition: str
    provider_metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class RegistrySnapshot:
    sites: dict[str, dict[str, Any]]
    source_streams: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class DiscoveryUpdateResult:
    sites_inserted: int
    sites_updated: int
    streams_inserted: int
    streams_updated: int
    streams_activated: int
    streams_inactivated: int
    blacklisted_preserved: int


@dataclass(frozen=True)
class DueSourceStream:
    source_stream_id: str
    site_id: str
    network_id: str
    provider_site_id: str | None
    provider_source_stream_id: str
    selected_rendition: str
    site_metadata: dict[str, Any]
    source_stream_metadata: dict[str, Any]
    latitude: float
    longitude: float
    altitude: float | None
    country: str | None
    corrected_latitude: float | None
    corrected_longitude: float | None
    corrected_altitude: float | None
    last_download_timestamp: datetime | None
    last_provider_image_marker: str | None
    ema_download_period: float | None


def get_network_registry(
    connection: psycopg.Connection[Any], network_id: str
) -> RegistrySnapshot:
    """Return the current site and stream registry for one network."""
    _ensure_network_exists(connection, network_id)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM site WHERE network_id = %s ORDER BY site_id",
            (network_id,),
        )
        sites = {row["site_id"]: dict(row) for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT ss.*
            FROM source_stream AS ss
            JOIN site AS s USING (site_id)
            WHERE s.network_id = %s
            ORDER BY ss.source_stream_id
            """,
            (network_id,),
        )
        streams = {
            row["source_stream_id"]: dict(row) for row in cursor.fetchall()
        }
    return RegistrySnapshot(sites=sites, source_streams=streams)


def apply_discovery_update(
    connection: psycopg.Connection[Any],
    network_id: str,
    sites: Sequence[DiscoveredSite],
    source_streams: Sequence[DiscoveredSourceStream],
) -> DiscoveryUpdateResult:
    """Reconcile one complete provider discovery snapshot atomically.

    Rediscovered streams become active, absent streams become inactive, and a
    blacklisted stream always remains blacklisted. Provider metadata JSON is a
    current snapshot and is therefore replaced rather than merged.
    """
    _validate_discovery_input(sites, source_streams)
    with connection.transaction():
        _ensure_network_exists(connection, network_id)
        current = get_network_registry(connection, network_id)
        _check_identifier_collisions(connection, network_id, sites, source_streams)

        site_inserted = site_updated = 0
        for site in sites:
            existed = site.site_id in current.sites
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO site (
                        site_id, network_id, provider_site_id, latitude, longitude,
                        altitude, country, provider_metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (site_id) DO UPDATE SET
                        provider_site_id = EXCLUDED.provider_site_id,
                        latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude,
                        altitude = EXCLUDED.altitude,
                        country = EXCLUDED.country,
                        provider_metadata = EXCLUDED.provider_metadata
                    """,
                    (
                        site.site_id,
                        network_id,
                        site.provider_site_id,
                        site.latitude,
                        site.longitude,
                        site.altitude,
                        site.country,
                        Jsonb(dict(site.provider_metadata)),
                    ),
                )
            if existed:
                site_updated += 1
            else:
                site_inserted += 1

        stream_inserted = stream_updated = stream_activated = 0
        blacklisted_preserved = 0
        discovered_stream_ids = {stream.source_stream_id for stream in source_streams}
        for stream in source_streams:
            previous = current.source_streams.get(stream.source_stream_id)
            if previous is None:
                next_status: StreamStatus = "active"
                stream_inserted += 1
            elif previous["status"] == "blacklisted":
                next_status = "blacklisted"
                blacklisted_preserved += 1
                stream_updated += 1
            else:
                next_status = "active"
                stream_updated += 1
                if previous["status"] == "inactive":
                    stream_activated += 1

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO source_stream (
                        source_stream_id, site_id, provider_source_stream_id,
                        selected_rendition, status, provider_metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_stream_id) DO UPDATE SET
                        selected_rendition = EXCLUDED.selected_rendition,
                        status = EXCLUDED.status,
                        provider_metadata = EXCLUDED.provider_metadata
                    """,
                    (
                        stream.source_stream_id,
                        stream.site_id,
                        stream.provider_source_stream_id,
                        stream.selected_rendition,
                        next_status,
                        Jsonb(dict(stream.provider_metadata)),
                    ),
                )

        missing_ids = set(current.source_streams) - discovered_stream_ids
        streams_inactivated = 0
        if missing_ids:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE source_stream
                    SET status = 'inactive'
                    WHERE source_stream_id = ANY(%s)
                      AND status = 'active'
                    """,
                    (list(missing_ids),),
                )
                streams_inactivated = cursor.rowcount
            blacklisted_preserved += sum(
                current.source_streams[stream_id]["status"] == "blacklisted"
                for stream_id in missing_ids
            )

    return DiscoveryUpdateResult(
        sites_inserted=site_inserted,
        sites_updated=site_updated,
        streams_inserted=stream_inserted,
        streams_updated=stream_updated,
        streams_activated=stream_activated,
        streams_inactivated=streams_inactivated,
        blacklisted_preserved=blacklisted_preserved,
    )


def set_source_stream_status(
    connection: psycopg.Connection[Any],
    source_stream_id: str,
    status: StreamStatus,
) -> None:
    """Apply an explicit operator status change, including blacklisting."""
    if status not in {"active", "inactive", "blacklisted"}:
        raise ValueError(f"unsupported source-stream status: {status}")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE source_stream SET status = %s WHERE source_stream_id = %s",
            (status, source_stream_id),
        )
        if cursor.rowcount != 1:
            raise RegistryRecordNotFoundError(
                f"source stream does not exist: {source_stream_id}"
            )


def get_due_source_streams(
    connection: psycopg.Connection[Any],
    network_id: str,
    minimum_ingestion_interval: timedelta,
    *,
    now: datetime | None = None,
) -> list[DueSourceStream]:
    """Return active streams whose last successful download is old enough."""
    if minimum_ingestion_interval < timedelta(0):
        raise ValueError("minimum ingestion interval cannot be negative")
    now = now or datetime.now(timezone.utc)
    _require_aware_datetime(now, "now")
    cutoff = now - minimum_ingestion_interval
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT
                ss.source_stream_id, ss.site_id, s.network_id,
                s.provider_site_id, ss.provider_source_stream_id,
                ss.selected_rendition, s.provider_metadata AS site_metadata,
                ss.provider_metadata AS source_stream_metadata,
                s.latitude, s.longitude, s.altitude, s.country,
                s.corrected_latitude, s.corrected_longitude,
                s.corrected_altitude, ss.last_download_timestamp,
                ss.last_provider_image_marker, ss.ema_download_period
            FROM source_stream AS ss
            JOIN site AS s USING (site_id)
            WHERE s.network_id = %s
              AND ss.status = 'active'
              AND (
                  ss.last_download_timestamp IS NULL
                  OR ss.last_download_timestamp <= %s
              )
            ORDER BY ss.last_download_timestamp NULLS FIRST,
                     ss.source_stream_id
            """,
            (network_id, cutoff),
        )
        return [DueSourceStream(**dict(row)) for row in cursor.fetchall()]


def record_provider_image_marker(
    connection: psycopg.Connection[Any],
    source_stream_id: str,
    provider_image_marker: str,
) -> None:
    """Record a handled marker without claiming a successful download."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE source_stream
            SET last_provider_image_marker = %s
            WHERE source_stream_id = %s
            """,
            (provider_image_marker, source_stream_id),
        )
        if cursor.rowcount != 1:
            raise RegistryRecordNotFoundError(
                f"source stream does not exist: {source_stream_id}"
            )


def record_successful_download(
    connection: psycopg.Connection[Any],
    source_stream_id: str,
    provider_image_marker: str,
    download_timestamp: datetime,
    *,
    ema_alpha: float,
    initial_ema_seconds: float = 300.0,
) -> float:
    """Atomically record a successful source download and update its EMA."""
    if not 0 <= ema_alpha <= 1:
        raise ValueError("ema_alpha must be between 0 and 1")
    if initial_ema_seconds < 0:
        raise ValueError("initial EMA cannot be negative")
    _require_aware_datetime(download_timestamp, "download timestamp")

    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT last_download_timestamp, ema_download_period
                FROM source_stream
                WHERE source_stream_id = %s
                FOR UPDATE
                """,
                (source_stream_id,),
            )
            previous = cursor.fetchone()
            if previous is None:
                raise RegistryRecordNotFoundError(
                    f"source stream does not exist: {source_stream_id}"
                )
            previous_timestamp, previous_ema = previous
            if previous_timestamp is None:
                next_ema = initial_ema_seconds
            else:
                elapsed = (download_timestamp - previous_timestamp).total_seconds()
                if elapsed < 0:
                    raise ValueError("download timestamp cannot move backwards")
                baseline = (
                    float(previous_ema)
                    if previous_ema is not None
                    else initial_ema_seconds
                )
                next_ema = ema_alpha * elapsed + (1 - ema_alpha) * baseline

            cursor.execute(
                """
                UPDATE source_stream
                SET last_provider_image_marker = %s,
                    last_download_timestamp = %s,
                    ema_download_period = %s
                WHERE source_stream_id = %s
                """,
                (
                    provider_image_marker,
                    download_timestamp,
                    next_ema,
                    source_stream_id,
                ),
            )
    return next_ema


def _ensure_network_exists(
    connection: psycopg.Connection[Any], network_id: str
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM network WHERE network_id = %s", (network_id,))
        if cursor.fetchone() is None:
            raise RegistryRecordNotFoundError(f"network does not exist: {network_id}")


def _validate_discovery_input(
    sites: Sequence[DiscoveredSite],
    source_streams: Sequence[DiscoveredSourceStream],
) -> None:
    site_ids = [site.site_id for site in sites]
    stream_ids = [stream.source_stream_id for stream in source_streams]
    if len(site_ids) != len(set(site_ids)):
        raise RegistryCollisionError("discovery contains duplicate site identifiers")
    if len(stream_ids) != len(set(stream_ids)):
        raise RegistryCollisionError("discovery contains duplicate stream identifiers")
    provider_site_keys = [
        site.provider_site_id for site in sites if site.provider_site_id is not None
    ]
    if len(provider_site_keys) != len(set(provider_site_keys)):
        raise RegistryCollisionError("discovery contains duplicate provider site identifiers")
    provider_stream_keys = [
        (stream.site_id, stream.provider_source_stream_id)
        for stream in source_streams
    ]
    if len(provider_stream_keys) != len(set(provider_stream_keys)):
        raise RegistryCollisionError(
            "discovery contains duplicate provider stream identifiers for one site"
        )
    unknown_site_ids = {stream.site_id for stream in source_streams} - set(site_ids)
    if unknown_site_ids:
        raise RegistryError(
            f"discovered streams reference sites absent from snapshot: "
            f"{sorted(unknown_site_ids)}"
        )


def _check_identifier_collisions(
    connection: psycopg.Connection[Any],
    network_id: str,
    sites: Sequence[DiscoveredSite],
    source_streams: Sequence[DiscoveredSourceStream],
) -> None:
    with connection.cursor(row_factory=dict_row) as cursor:
        for site in sites:
            cursor.execute(
                "SELECT network_id, provider_site_id FROM site WHERE site_id = %s",
                (site.site_id,),
            )
            existing = cursor.fetchone()
            if existing is not None and (
                existing["network_id"] != network_id
                or existing["provider_site_id"] != site.provider_site_id
            ):
                raise RegistryCollisionError(
                    f"site identifier already belongs to another provider entity: "
                    f"{site.site_id}"
                )
            if site.provider_site_id is not None:
                cursor.execute(
                    """
                    SELECT site_id
                    FROM site
                    WHERE network_id = %s AND provider_site_id = %s
                    """,
                    (network_id, site.provider_site_id),
                )
                provider_match = cursor.fetchone()
                if (
                    provider_match is not None
                    and provider_match["site_id"] != site.site_id
                ):
                    raise RegistryCollisionError(
                        "provider site identifier is already assigned to another "
                        f"internal identifier: {site.provider_site_id}"
                    )

        for stream in source_streams:
            cursor.execute(
                """
                SELECT site_id, provider_source_stream_id
                FROM source_stream
                WHERE source_stream_id = %s
                """,
                (stream.source_stream_id,),
            )
            existing = cursor.fetchone()
            if existing is not None and (
                existing["site_id"] != stream.site_id
                or existing["provider_source_stream_id"]
                != stream.provider_source_stream_id
            ):
                raise RegistryCollisionError(
                    f"stream identifier already belongs to another provider entity: "
                    f"{stream.source_stream_id}"
                )
            cursor.execute(
                """
                SELECT source_stream_id
                FROM source_stream
                WHERE site_id = %s AND provider_source_stream_id = %s
                """,
                (stream.site_id, stream.provider_source_stream_id),
            )
            provider_match = cursor.fetchone()
            if (
                provider_match is not None
                and provider_match["source_stream_id"] != stream.source_stream_id
            ):
                raise RegistryCollisionError(
                    "provider stream identifier is already assigned to another "
                    f"internal identifier: {stream.provider_source_stream_id}"
                )


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
