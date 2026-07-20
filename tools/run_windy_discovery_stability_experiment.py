"""Run consecutive fresh Windy discoveries and report registry transitions."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from config.deployment_config import DatabaseConfig
from discovery.windy.windy_discovery_workflow import run_discovery


FORTY_KM_COUNTRIES = {"AT", "CH", "CZ", "DE", "ES", "GB", "IT", "NO"}
FIFTY_KM_COUNTRIES = {"FI", "FR"}


def registry_snapshot() -> dict[str, dict[str, str]]:
    database = DatabaseConfig.from_environment()
    with psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.name,
        user=database.user,
        password=database.read_password(),
        connect_timeout=5,
        row_factory=dict_row,
    ) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ss.provider_source_stream_id AS provider_id, ss.status, s.country
            FROM source_stream ss
            JOIN site s USING (site_id)
            WHERE s.network_id = 'win'
            """
        )
        return {
            row["provider_id"]: {"status": row["status"], "country": row["country"]}
            for row in cursor.fetchall()
        }


def discovery_mode(country: str) -> str:
    if country in FORTY_KM_COUNTRIES:
        return "40 km discs"
    if country in FIFTY_KM_COUNTRIES:
        return "50 km discs"
    return "country listing"


def transition_report(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
) -> dict[str, Any]:
    new = sorted(set(after) - set(before))
    reactivated = sorted(
        provider_id
        for provider_id in set(before) & set(after)
        if before[provider_id]["status"] == "inactive"
        and after[provider_id]["status"] == "active"
    )
    inactivated = sorted(
        provider_id
        for provider_id in set(before) & set(after)
        if before[provider_id]["status"] == "active"
        and after[provider_id]["status"] == "inactive"
    )

    def describe(ids: list[str]) -> dict[str, Any]:
        records = []
        for provider_id in ids:
            row = after[provider_id] if provider_id in after else before[provider_id]
            records.append(
                {
                    "provider_id": provider_id,
                    "country": row["country"],
                    "mode": discovery_mode(row["country"]),
                }
            )
        grouped = Counter((record["mode"], record["country"]) for record in records)
        return {
            "count": len(records),
            "by_mode_and_country": [
                {"mode": mode, "country": country, "count": count}
                for (mode, country), count in sorted(grouped.items())
            ],
            "webcams": records,
        }

    return {
        "new": describe(new),
        "reactivated": describe(reactivated),
        "inactivated": describe(inactivated),
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--wait-s", type=float, default=120)
    parser.add_argument("--delay-s", type=float, default=0.1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.passes < 1 or args.wait_s < 0 or args.delay_s < 0:
        parser.error("passes must be positive; delays cannot be negative")

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "passes_requested": args.passes,
        "wait_s": args.wait_s,
        "request_delay_s": args.delay_s,
        "passes": [],
    }
    for pass_number in range(1, args.passes + 1):
        before = registry_snapshot()
        cache_file = args.cache_dir / f"pass-{pass_number}.json"
        os.environ["WINDY_DISCOVERY_CACHE_FILE"] = str(cache_file)
        os.environ["WINDY_REQUEST_DELAY_S"] = str(args.delay_s)
        started = time.monotonic()
        result = run_discovery()
        duration_s = time.monotonic() - started
        after = registry_snapshot()
        pass_report = {
            "pass": pass_number,
            "completed_at": datetime.now(UTC).isoformat(),
            "duration_s": round(duration_s, 3),
            "cache_file": str(cache_file),
            "update": asdict(result),
            "transitions": transition_report(before, after),
        }
        report["passes"].append(pass_report)
        write_report(args.output, report)
        print(json.dumps(pass_report, sort_keys=True), flush=True)
        if pass_number < args.passes:
            time.sleep(args.wait_s)
    report["completed_at"] = datetime.now(UTC).isoformat()
    write_report(args.output, report)


if __name__ == "__main__":
    main()
