"""Acquire and optionally publish a bounded country-filtered Windy sample."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import timedelta
import json
from typing import Any

import psycopg

from config.deployment_config import (
    DatabaseConfig,
    EUMETNET_MEMBER_COUNTRIES,
    MqttConfig,
    S3Config,
    TransformationConfig,
    WindyIngestionConfig,
)
from database.registry_queries import (
    DueSourceStream,
    apply_ingestion_state_updates,
    get_due_source_streams,
)
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.shared.source_processing import (
    IngestionJobResult,
    process_job,
)
from ingestion.windy.windy_image_access import WindyImageClient
from storage.s3_storage import S3Storage


NETWORK_ID = "win"


@dataclass(frozen=True)
class WindyIngestionResult:
    dry_run: bool
    publish: bool
    countries: tuple[str, ...]
    selected: int
    outcomes: dict[str, int]
    jobs: tuple[IngestionJobResult, ...]


def run_ingestion(
    countries: tuple[str, ...],
    *,
    limit: int | None = None,
    dry_run: bool = False,
    publish: bool = False,
) -> WindyIngestionResult:
    if dry_run and publish:
        raise ValueError("--dry-run and --publish cannot be combined")
    normalized_countries = _validate_countries(countries)
    config = WindyIngestionConfig.from_environment()
    transformation = TransformationConfig.from_environment()
    selected_limit = limit or config.default_limit
    if selected_limit < 1:
        raise ValueError("limit must be positive")
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
        client = stack.enter_context(
            WindyImageClient(
                config.read_api_key(),
                request_timeout_s=config.request_timeout_s,
                image_timeout_s=config.image_download_timeout_s,
                max_image_bytes=config.image_max_bytes,
                request_delay_s=config.request_delay_s,
                freshness_query_retry_count=config.freshness_query_retry_count,
                download_retry_count=config.download_retry_count,
                retry_backoff_s=config.retry_backoff_s,
            )
        )
        storage = S3Storage(S3Config.from_environment()) if publish else None
        publisher = (
            stack.enter_context(MqttPublisher(MqttConfig.from_environment()))
            if publish
            else None
        )
        jobs = _select_country_sample(
            connection,
            normalized_countries,
            selected_limit,
            timedelta(seconds=config.minimum_ingestion_interval_s),
            polling_interval_factor=config.polling_interval_factor,
            minimum_polling_interval=timedelta(
                seconds=config.minimum_polling_interval_s
            ),
        )
        results = tuple(
            process_job(
                client,
                job,
                dry_run=dry_run,
                minimum_period_seconds=config.minimum_ingestion_interval_s,
                transformation=transformation,
                storage=storage,
                publisher=publisher,
            )
            for job in jobs
        )
        if not dry_run:
            apply_ingestion_state_updates(
                connection,
                [result.state_update for result in results if result.state_update],
                apply_period_estimate=True,
            )
            connection.commit()
    outcomes = dict(sorted(Counter(item.outcome for item in results).items()))
    return WindyIngestionResult(
        dry_run, publish, normalized_countries, len(jobs), outcomes, results
    )


def _validate_countries(countries: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(code.strip().upper() for code in countries))
    allowed = set(EUMETNET_MEMBER_COUNTRIES)
    if not normalized or any(code not in allowed for code in normalized):
        raise ValueError("countries must be EUMETNET ISO alpha-2 codes")
    return normalized


def _select_country_sample(
    connection: psycopg.Connection[Any],
    countries: tuple[str, ...],
    limit: int,
    minimum_ingestion_interval: timedelta,
    *,
    polling_interval_factor: float = 0.7,
    minimum_polling_interval: timedelta = timedelta(0),
) -> list[DueSourceStream]:
    """Select a small deterministic sample without starving later countries."""
    selected: list[DueSourceStream] = []
    quotient, remainder = divmod(limit, len(countries))
    for index, country in enumerate(countries):
        country_limit = quotient + (index < remainder)
        if country_limit:
            selected.extend(
                get_due_source_streams(
                    connection,
                    NETWORK_ID,
                    minimum_ingestion_interval,
                    polling_interval_factor=polling_interval_factor,
                    minimum_polling_interval=minimum_polling_interval,
                    countries=(country,),
                    limit=country_limit,
                )
            )
    return selected


def _parse_countries(value: str) -> tuple[str, ...]:
    return tuple(code for code in value.split(",") if code.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--countries",
        required=True,
        help="comma-separated EUMETNET ISO alpha-2 codes, for example DK,MT",
    )
    parser.add_argument("--limit", type=int, help="maximum streams; defaults to 10")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and transform in memory without database, S3, or MQTT writes",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="transform, upload to configured S3, then publish MQTT N0V0",
    )
    args = parser.parse_args()
    result = run_ingestion(
        _parse_countries(args.countries),
        limit=args.limit,
        dry_run=args.dry_run,
        publish=args.publish,
    )
    payload = asdict(result)
    for job_payload in payload["jobs"]:
        job_payload.pop("period_estimate_candidate", None)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
