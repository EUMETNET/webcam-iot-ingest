"""Send one Alertmanager summary for source streams seen during maintenance."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

NETWORK_NAMES = {"win": "Windy", "fin": "Fintraffic", "ska": "Skaping"}


def read_discovery_result(path: Path) -> int:
    """Read the final source-stream count emitted by one discovery command."""
    source_streams: int | None = None
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("source_streams"), int):
            source_streams = value["source_streams"]
    if source_streams is None:
        raise ValueError(f"no discovery result containing source_streams in {path}")
    return source_streams


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
    notify = subparsers.add_parser("notify")
    notify.add_argument("windy_result", type=Path)
    notify.add_argument("fintraffic_result", type=Path)
    notify.add_argument("skaping_result", type=Path)
    args = parser.parse_args()

    counts = {
        "win": read_discovery_result(args.windy_result),
        "fin": read_discovery_result(args.fintraffic_result),
        "ska": read_discovery_result(args.skaping_result),
    }
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
