"""Send one Alertmanager summary for source streams seen during maintenance."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from prometheus_client.parser import text_string_to_metric_families


NETWORK_NAMES = {"win": "Windy", "fin": "Fintraffic", "ska": "Skaping"}
METRIC_NAME = "webcam_discovery_sources_seen_total"


def read_discovery_counts(gateway_url: str, *, timeout_s: float = 5) -> dict[str, int]:
    """Read the cumulative discovery counters currently retained by Pushgateway."""
    with urlopen(f"{gateway_url.rstrip('/')}/metrics", timeout=timeout_s) as response:
        exposition = response.read().decode("utf-8")
    counts = dict.fromkeys(NETWORK_NAMES, 0)
    for family in text_string_to_metric_families(exposition):
        for sample in family.samples:
            if sample.name != METRIC_NAME:
                continue
            network = sample.labels.get("source_network")
            if network in counts:
                counts[network] = int(sample.value)
    return counts


def count_differences(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """Return non-negative per-network counter changes."""
    return {
        network: max(0, after.get(network, 0) - before.get(network, 0))
        for network in NETWORK_NAMES
    }


def send_summary_alert(
    alertmanager_url: str,
    counts: dict[str, int],
    *,
    timeout_s: float = 5,
    active_minutes: int = 10,
) -> None:
    """Submit one bounded maintenance-summary alert to Alertmanager."""
    now = datetime.now(timezone.utc)
    total = sum(counts.values())
    detail = ", ".join(
        f"{NETWORK_NAMES[network]}: {counts[network]}" for network in NETWORK_NAMES
    )
    payload = [
        {
            "labels": {
                "alertname": "WebcamMaintenanceSequenceSucceeded",
                "severity": "info",
                "source_network": "all",
            },
            "annotations": {
                "summary": f"Maintenance succeeded; discovered {total} webcam streams",
                "description": (
                    f"Streams seen during this run — {detail}; total: {total}."
                ),
            },
            "startsAt": now.isoformat(),
            "endsAt": (now + timedelta(minutes=active_minutes)).isoformat(),
        }
    ]
    request = Request(
        f"{alertmanager_url.rstrip('/')}/api/v2/alerts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_s):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("path", type=Path)
    notify = subparsers.add_parser("notify")
    notify.add_argument("path", type=Path)
    args = parser.parse_args()

    gateway_url = os.getenv("DISCOVERY_METRICS_GATEWAY_URL", "http://pushgateway:9091")
    if args.command == "snapshot":
        args.path.write_text(json.dumps(read_discovery_counts(gateway_url)))
        return

    before = json.loads(args.path.read_text())
    counts = count_differences(before, read_discovery_counts(gateway_url))
    alertmanager_url = os.getenv("ALERTMANAGER_URL", "http://alertmanager:9093")
    send_summary_alert(alertmanager_url, counts)
    print(
        json.dumps(
            {
                "maintenance_success_alert": {
                    "counts": counts,
                    "total": sum(counts.values()),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
