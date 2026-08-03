"""Explicitly reset learned source-stream periods for selected networks."""

from __future__ import annotations

import argparse
import json

import psycopg

from config.deployment_config import DatabaseConfig
from database.registry_queries import reset_network_period_estimates


NETWORKS = ("win", "fin", "ska")
CONFIRMATION = "RESET_PERIOD_ESTIMATES"


def reset_period_estimates(networks: tuple[str, ...]) -> dict[str, int]:
    database = DatabaseConfig.from_environment()
    with psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.name,
        user=database.user,
        password=database.read_password(),
    ) as connection:
        return {
            network: reset_network_period_estimates(connection, network)
            for network in networks
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--networks",
        default=",".join(NETWORKS),
        help="comma-separated internal network IDs: win,fin,ska",
    )
    parser.add_argument("--confirm")
    args = parser.parse_args()
    networks = tuple(dict.fromkeys(item.strip() for item in args.networks.split(",")))
    if not networks or any(network not in NETWORKS for network in networks):
        parser.error("--networks must contain only win, fin, and ska")
    if args.confirm != CONFIRMATION:
        parser.error(f"reset requires --confirm {CONFIRMATION}")
    print(
        json.dumps(
            {
                "period_estimate_reset": reset_period_estimates(networks),
                "provider_timestamps_preserved": True,
                "download_timestamps_preserved": True,
                "processed_timestamps_preserved": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
