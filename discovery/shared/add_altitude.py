"""Backfill missing site altitudes from the configured elevation service."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from config.deployment_config import AltitudeConfig, DatabaseConfig
from discovery.shared.altitude_lookup import AltitudeClient, AltitudeLookupError


@dataclass(frozen=True)
class AltitudeEnrichmentResult:
    network: str
    dry_run: bool
    eligible: int
    resolved: int
    unresolved: int
    updated: int


def enrich_missing_altitudes(
    connection: psycopg.Connection[Any],
    network: str,
    client: AltitudeClient,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int = 100,
) -> AltitudeEnrichmentResult:
    if not network:
        raise ValueError("network cannot be empty")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch size must be between 1 and 100")

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT 1 FROM network WHERE network_id = %s", (network,))
        if cursor.fetchone() is None:
            raise ValueError(f"network does not exist: {network}")
        query = """
            SELECT site_id, latitude, longitude
            FROM site
            WHERE network_id = %s AND altitude IS NULL
            ORDER BY site_id
        """
        parameters: tuple[object, ...] = (network,)
        if limit is not None:
            query += " LIMIT %s"
            parameters += (limit,)
        cursor.execute(query, parameters)
        sites = [dict(row) for row in cursor.fetchall()]

    resolved: list[tuple[str, float, float, float]] = []
    unresolved = 0
    for offset in range(0, len(sites), batch_size):
        batch = sites[offset : offset + batch_size]
        coordinates = [(float(row["latitude"]), float(row["longitude"])) for row in batch]
        try:
            values = client.lookup(coordinates)
        except AltitudeLookupError:
            unresolved += len(batch)
            continue
        for row, altitude in zip(batch, values, strict=True):
            if altitude is None:
                unresolved += 1
                continue
            resolved.append(
                (
                    str(row["site_id"]),
                    float(row["latitude"]),
                    float(row["longitude"]),
                    altitude,
                )
            )

    updated = 0
    if not dry_run and resolved:
        with connection.transaction(), connection.cursor() as cursor:
            for site_id, latitude, longitude, altitude in resolved:
                cursor.execute(
                    """
                    UPDATE site SET altitude = %s
                    WHERE site_id = %s AND network_id = %s
                      AND altitude IS NULL
                      AND latitude = %s AND longitude = %s
                    """,
                    (altitude, site_id, network, latitude, longitude),
                )
                updated += cursor.rowcount

    return AltitudeEnrichmentResult(
        network=network,
        dry_run=dry_run,
        eligible=len(sites),
        resolved=len(resolved),
        unresolved=unresolved,
        updated=updated,
    )


def run(
    network: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int | None = None,
) -> AltitudeEnrichmentResult:
    altitude = AltitudeConfig.from_environment()
    if not altitude.enabled:
        return AltitudeEnrichmentResult(network, dry_run, 0, 0, 0, 0)
    database = DatabaseConfig.from_environment()
    selected_batch_size = batch_size or altitude.batch_size
    if limit is not None and limit > altitude.max_sites_per_run:
        raise ValueError(
            f"limit cannot exceed configured maximum {altitude.max_sites_per_run}"
        )
    selected_limit = limit or altitude.max_sites_per_run
    with AltitudeClient(
        altitude.provider_url,
        timeout_s=altitude.request_timeout_s,
        request_delay_s=altitude.request_delay_s,
        max_attempts=altitude.max_attempts,
    ) as client, psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.name,
        user=database.user,
        password=database.read_password(),
        connect_timeout=5,
    ) as connection:
        return enrich_missing_altitudes(
            connection,
            network,
            client,
            dry_run=dry_run,
            limit=selected_limit,
            batch_size=selected_batch_size,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int)
    args = parser.parse_args()
    result = run(
        args.network,
        dry_run=args.dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
