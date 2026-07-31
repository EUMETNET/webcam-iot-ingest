"""Acquire and optionally publish a bounded Fintraffic preset sample."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import timedelta
import json

import psycopg

from config.deployment_config import (
    DatabaseConfig,
    FintrafficIngestionConfig,
    MqttConfig,
    S3Config,
    TransformationConfig,
)
from database.registry_queries import apply_ingestion_state_updates, get_due_source_streams
from ingestion.fintraffic.fintraffic_image_access import FintrafficImageClient
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.windy.windy_ingestion_workflow import (
    WindyIngestionJobResult,
    _process_job,
)
from storage.s3_storage import S3Storage


@dataclass(frozen=True)
class FintrafficIngestionResult:
    dry_run: bool
    publish: bool
    selected: int
    freshness_presets: int
    outcomes: dict[str, int]
    jobs: tuple[WindyIngestionJobResult, ...]


def run_ingestion(
    *,
    limit: int | None = None,
    dry_run: bool = False,
    publish: bool = False,
) -> FintrafficIngestionResult:
    if dry_run and publish:
        raise ValueError("--dry-run and --publish cannot be combined")
    config = FintrafficIngestionConfig.from_environment()
    selected_limit = limit or config.default_limit
    database = DatabaseConfig.from_environment()
    with ExitStack() as stack:
        connection = stack.enter_context(
            psycopg.connect(
                host=database.host,
                port=database.port,
                dbname=database.name,
                user=database.user,
                password=database.read_password(),
                connect_timeout=5,
            )
        )
        client = stack.enter_context(_new_client(config))
        freshness_presets = client.refresh()
        storage = S3Storage(S3Config.from_environment()) if publish else None
        publisher = (
            stack.enter_context(MqttPublisher(MqttConfig.from_environment()))
            if publish
            else None
        )
        jobs = get_due_source_streams(
            connection,
            "fin",
            timedelta(seconds=config.minimum_ingestion_interval_s),
            polling_interval_factor=config.polling_interval_factor,
            limit=selected_limit,
        )
        results = tuple(
            _process_job(
                client,
                job,
                dry_run=dry_run,
                ema_alpha=config.ema_alpha,
                transformation=TransformationConfig.from_environment(),
                storage=storage,
                publisher=publisher,
            )
            for job in jobs
        )
        if not dry_run:
            apply_ingestion_state_updates(
                connection,
                [result.state_update for result in results if result.state_update],
                apply_ema=True,
            )
            connection.commit()
    outcomes = dict(sorted(Counter(item.outcome for item in results).items()))
    return FintrafficIngestionResult(
        dry_run, publish, len(jobs), freshness_presets, outcomes, results
    )


def _new_client(
    config: FintrafficIngestionConfig, *, observer=None, request_gate=None
) -> FintrafficImageClient:
    return FintrafficImageClient(
        config.user_header,
        data_url=config.data_url,
        image_base_url=config.image_base_url,
        request_timeout_s=config.request_timeout_s,
        image_timeout_s=config.image_download_timeout_s,
        max_image_bytes=config.image_max_bytes,
        freshness_query_retry_count=config.freshness_query_retry_count,
        download_retry_count=config.download_retry_count,
        retry_backoff_s=config.retry_backoff_s,
        request_gate=request_gate,
        observer=observer,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asdict(
                run_ingestion(
                    limit=args.limit, dry_run=args.dry_run, publish=args.publish
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
