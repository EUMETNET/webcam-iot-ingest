"""Replay a bounded batch of durable pending S3/MQTT publications."""

import argparse
from collections import Counter
import json

import psycopg

from config.deployment_config import DatabaseConfig, MqttConfig, S3Config
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.shared.publication_outbox import drain_publication_outbox
from storage.s3_storage import S3Storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    database = DatabaseConfig.from_environment()
    with psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=database.name,
        user=database.user,
        password=database.read_password(),
        connect_timeout=5,
    ) as connection, MqttPublisher(MqttConfig.from_environment()) as publisher:
        results = drain_publication_outbox(
            connection,
            S3Storage(S3Config.from_environment()),
            publisher,
            limit=args.limit,
        )
    print(
        json.dumps(
            {
                "selected": len(results),
                "outcomes": dict(sorted(Counter(r.outcome for r in results).items())),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
