"""Acquire and optionally publish a bounded country-filtered Windy sample."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import time
from typing import Any, Callable, Literal

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
    EmaUpdateCandidate,
    PendingPublication,
    get_due_source_streams,
    record_download_and_enqueue_publication,
    record_freshness_query,
    record_provider_image_marker,
    record_successful_download,
)
from ingestion.notification.mqtt_publisher import MqttPublicationError, MqttPublisher
from ingestion.shared.ingestion_core import process_source_image
from ingestion.shared.ingestion_core import prepare_publication
from ingestion.shared.publication_outbox import deliver_publication
from ingestion.shared.source_image_validation import (
    InvalidSourceImageError,
    validate_source_image,
)
from ingestion.windy.windy_image_access import WindyImageAccessError, WindyImageClient
from storage.s3_storage import S3Storage, S3StorageError


NETWORK_ID = "win"
JobOutcome = Literal[
    "downloaded",
    "published",
    "unchanged",
    "provider_error",
    "download_error",
    "throttled",
    "invalid_image",
    "transformation_error",
    "storage_error",
    "mqtt_error",
]


@dataclass(frozen=True)
class WindyIngestionJobResult:
    source_stream_id: str
    country: str | None
    name: str | None
    outcome: JobOutcome
    provider_marker: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    image_format: str | None = None
    color_mode: str | None = None
    ema_download_period: float | None = None
    derived_size_bytes: int | None = None
    derived_width: int | None = None
    derived_height: int | None = None
    image_signature: float | None = None
    image_id: str | None = None
    object_key: str | None = None
    mqtt_topic: str | None = None
    ema_update_candidate: EmaUpdateCandidate | None = None


@dataclass(frozen=True)
class WindyIngestionResult:
    dry_run: bool
    publish: bool
    countries: tuple[str, ...]
    selected: int
    outcomes: dict[str, int]
    jobs: tuple[WindyIngestionJobResult, ...]


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
        )
        results = tuple(
            _process_job(
                connection,
                client,
                job,
                dry_run=dry_run,
                ema_alpha=config.ema_alpha,
                transformation=transformation,
                storage=storage,
                publisher=publisher,
            )
            for job in jobs
        )
    outcomes = dict(sorted(Counter(item.outcome for item in results).items()))
    return WindyIngestionResult(
        dry_run, publish, normalized_countries, len(jobs), outcomes, results
    )


def _process_job(
    connection: psycopg.Connection[Any],
    client: WindyImageClient,
    job: DueSourceStream,
    *,
    dry_run: bool,
    ema_alpha: float,
    transformation: TransformationConfig | None = None,
    storage: S3Storage | None = None,
    publisher: MqttPublisher | None = None,
    stage_observer: Callable[[str, str, float], None] | None = None,
    event_observer: Callable[[str, dict[str, object]], None] | None = None,
    defer_ema_update: bool = False,
) -> WindyIngestionJobResult:
    job_started = time.monotonic()
    name_value = job.source_stream_metadata.get("title")
    name = name_value if isinstance(name_value, str) else None
    download_timestamp: datetime | None = None
    provider_update_timestamp: datetime | None = None
    provider_to_download_s: float | None = None

    def finish(outcome: JobOutcome, **values: Any) -> WindyIngestionJobResult:
        finished_at = datetime.now(UTC)
        if event_observer is not None:
            event_observer(
                "job_completed",
                {"outcome": outcome, "duration_s": time.monotonic() - job_started},
            )
            failure = {
                "provider_error": ("provider_access", "provider_error"),
                "download_error": ("source_download", "download_error"),
                "throttled": ("provider_access", "throttled"),
                "invalid_image": ("source_validation", "invalid_image"),
                "transformation_error": ("transformation", "transformation_error"),
                "storage_error": ("s3_upload", "s3_upload"),
                "mqtt_error": ("mqtt_publish", "mqtt_publish"),
            }.get(outcome)
            if failure is not None:
                event_observer(
                    "failure", {"stage": failure[0], "reason": failure[1]}
                )
            if outcome == "published" and download_timestamp is not None:
                download_to_end_s = (finished_at - download_timestamp).total_seconds()
                event_observer(
                    "image_latency",
                    {"measure": "download_to_job_end", "duration_s": download_to_end_s},
                )
                if provider_to_download_s is not None:
                    event_observer(
                        "image_latency",
                        {
                            "measure": "provider_to_job_end",
                            "duration_s": provider_to_download_s + download_to_end_s,
                        },
                    )
        return _job_result(job, name, outcome, **values)

    try:
        reference = client.get_current_image(
            job.provider_source_stream_id, job.selected_rendition
        )
    except WindyImageAccessError as error:
        if not dry_run:
            record_freshness_query(connection, job.source_stream_id, datetime.now(UTC))
            connection.commit()
        return finish("throttled" if error.throttled else "provider_error")
    if not dry_run:
        record_freshness_query(connection, job.source_stream_id, datetime.now(UTC))
        connection.commit()
    if reference.marker == job.last_provider_image_marker:
        return finish("unchanged", provider_marker=reference.marker)
    try:
        content = client.download(reference.image_url)
    except WindyImageAccessError as error:
        return finish(
            "throttled" if error.throttled else "download_error",
            provider_marker=reference.marker,
        )
    try:
        source = validate_source_image(content)
    except InvalidSourceImageError:
        if not dry_run:
            record_provider_image_marker(connection, job.source_stream_id, reference.marker)
        return finish(
            "invalid_image",
            provider_marker=reference.marker,
            size_bytes=len(content),
        )

    download_timestamp = datetime.now(UTC)
    provider_update_timestamp = _parse_provider_timestamp(reference.marker)
    if provider_update_timestamp is not None:
        candidate_latency = (download_timestamp - provider_update_timestamp).total_seconds()
        if candidate_latency >= 0:
            provider_to_download_s = candidate_latency
            if event_observer is not None:
                event_observer(
                    "image_latency",
                    {
                        "measure": "provider_to_download",
                        "duration_s": provider_to_download_s,
                    },
                )
    selected_transformation = transformation or TransformationConfig.from_environment()
    if storage is not None and publisher is not None:
        transformation_started = time.monotonic()
        try:
            prepared = prepare_publication(
                job=job,
                source=source,
                download_timestamp=download_timestamp,
                provider_update_timestamp=provider_update_timestamp,
                transformation=selected_transformation,
                storage=storage,
            )
        except Exception:
            if stage_observer is not None:
                stage_observer(
                    "transformation", "failure", time.monotonic() - transformation_started
                )
            if event_observer is not None:
                event_observer(
                    "transformation",
                    {"version": selected_transformation.version, "outcome": "failure"},
                )
            ema_value: float | EmaUpdateCandidate | None = None
            if not dry_run:
                ema_value = record_successful_download(
                    connection,
                    job.source_stream_id,
                    reference.marker,
                    download_timestamp,
                    ema_alpha=ema_alpha,
                    defer_ema_update=defer_ema_update,
                )
            return finish(
                "transformation_error",
                provider_marker=reference.marker,
                ema_update_candidate=(
                    ema_value if isinstance(ema_value, EmaUpdateCandidate) else None
                ),
            )
        if stage_observer is not None:
            stage_observer(
                "transformation", "success", time.monotonic() - transformation_started
            )
        if event_observer is not None:
            event_observer(
                "transformation",
                {"version": selected_transformation.version, "outcome": "success"},
            )
            event_observer(
                "derived_image",
                {
                    "version": selected_transformation.version,
                    "size_bytes": prepared.derived.size_bytes,
                    "width": prepared.derived.width,
                    "height": prepared.derived.height,
                },
            )
            event_observer(
                "mqtt_payload",
                {
                    "version": selected_transformation.version,
                    "size_bytes": len(
                        json.dumps(
                            prepared.notification,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                },
            )
        database_started = time.monotonic()
        ema_value = record_download_and_enqueue_publication(
            connection,
            job.source_stream_id,
            reference.marker,
            download_timestamp,
            ema_alpha=ema_alpha,
            image_id=prepared.image_id,
            object_key=prepared.object_key,
            derived_content=prepared.derived.content,
            notification=prepared.notification,
            defer_ema_update=defer_ema_update,
        )
        connection.commit()
        if stage_observer is not None:
            stage_observer(
                "database_enqueue", "success", time.monotonic() - database_started
            )
        delivery = deliver_publication(
            connection,
            storage,
            publisher,
            PendingPublication(
                prepared.image_id,
                job.source_stream_id,
                reference.marker,
                download_timestamp,
                prepared.object_key,
                prepared.derived.content,
                prepared.notification,
                "pending_s3",
                0,
                None,
            ),
        )
        return finish(
            delivery.outcome,
            provider_marker=reference.marker,
            size_bytes=source.size_bytes,
            width=source.width,
            height=source.height,
            image_format=source.format,
            color_mode=source.color_mode,
            ema_download_period=(ema_value if isinstance(ema_value, float) else None),
            ema_update_candidate=(
                ema_value if isinstance(ema_value, EmaUpdateCandidate) else None
            ),
            derived_size_bytes=prepared.derived.size_bytes,
            derived_width=prepared.derived.width,
            derived_height=prepared.derived.height,
            image_signature=prepared.derived.image_signature,
            image_id=prepared.image_id,
            object_key=prepared.object_key,
            mqtt_topic=(
                f"{MqttConfig.from_environment().topic_prefix}/{selected_transformation.version}"
                if delivery.outcome == "published"
                else None
            ),
        )

    ema = None
    if not dry_run:
        ema = record_successful_download(
            connection,
            job.source_stream_id,
            reference.marker,
            download_timestamp,
            ema_alpha=ema_alpha,
        )
    transformation_started = time.monotonic()
    try:
        publication = process_source_image(
            job=job,
            source=source,
            download_timestamp=download_timestamp,
            provider_update_timestamp=provider_update_timestamp,
            transformation=selected_transformation,
            storage=storage,
            publisher=publisher,
        )
    except S3StorageError:
        if stage_observer is not None:
            stage_observer(
                "s3_upload", "failure", time.monotonic() - transformation_started
            )
        return finish("storage_error", provider_marker=reference.marker)
    except MqttPublicationError:
        if stage_observer is not None:
            stage_observer(
                "mqtt_publish", "failure", time.monotonic() - transformation_started
            )
        return finish("mqtt_error", provider_marker=reference.marker)
    except Exception:
        if stage_observer is not None:
            stage_observer(
                "transformation", "failure", time.monotonic() - transformation_started
            )
        if event_observer is not None:
            event_observer(
                "transformation",
                {"version": selected_transformation.version, "outcome": "failure"},
            )
        return finish("transformation_error", provider_marker=reference.marker)
    if stage_observer is not None:
        stage_observer(
            "transformation", "success", time.monotonic() - transformation_started
        )
    if event_observer is not None:
        event_observer(
            "transformation",
            {"version": selected_transformation.version, "outcome": "success"},
        )
        event_observer(
            "derived_image",
            {
                "version": selected_transformation.version,
                "size_bytes": publication.derived.size_bytes,
                "width": publication.derived.width,
                "height": publication.derived.height,
            },
        )
    return finish(
        "published" if storage is not None else "downloaded",
        provider_marker=reference.marker,
        size_bytes=source.size_bytes,
        width=source.width,
        height=source.height,
        image_format=source.format,
        color_mode=source.color_mode,
        ema_download_period=ema,
        derived_size_bytes=publication.derived.size_bytes,
        derived_width=publication.derived.width,
        derived_height=publication.derived.height,
        image_signature=publication.derived.image_signature,
        image_id=publication.image_id,
        object_key=publication.object_key,
        mqtt_topic=publication.mqtt_topic,
    )


def _job_result(
    job: DueSourceStream,
    name: str | None,
    outcome: JobOutcome,
    **values: Any,
) -> WindyIngestionJobResult:
    return WindyIngestionJobResult(
        source_stream_id=job.source_stream_id,
        country=job.country,
        name=name,
        outcome=outcome,
        **values,
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
                    countries=(country,),
                    limit=country_limit,
                )
            )
    return selected


def _parse_provider_timestamp(marker: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(marker.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None


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
        job_payload.pop("ema_update_candidate", None)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
