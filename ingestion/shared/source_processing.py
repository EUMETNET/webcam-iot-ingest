"""Provider-neutral processing of one due source-stream job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import time
from typing import Any, Callable, Literal

from config.deployment_config import TransformationConfig
from database.registry_queries import (
    DueSourceStream,
    IngestionStateUpdate,
    PeriodEstimateCandidate,
    build_period_estimate_candidate,
)
from ingestion.notification.mqtt_publisher import MqttPublicationError, MqttPublisher
from ingestion.shared.ingestion_core import (
    PublicationResult,
    prepare_publication,
    process_source_image,
)
from ingestion.shared.provider_access import (
    ProviderImageAccessError,
    ProviderImageClient,
)
from ingestion.shared.source_image_validation import (
    InvalidSourceImageError,
    validate_source_image,
)
from storage.s3_storage import S3Storage, S3StorageError

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
class IngestionJobResult:
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
    estimated_source_stream_period: float | None = None
    derived_size_bytes: int | None = None
    derived_width: int | None = None
    derived_height: int | None = None
    image_signature: float | None = None
    image_id: str | None = None
    object_key: str | None = None
    mqtt_topic: str | None = None
    period_estimate_candidate: PeriodEstimateCandidate | None = None
    state_update: IngestionStateUpdate | None = None


def process_job(
    client: ProviderImageClient,
    job: DueSourceStream,
    *,
    dry_run: bool,
    minimum_period_seconds: float,
    direct_replacement_modulus: int = 250,
    transformation: TransformationConfig | None = None,
    storage: S3Storage | None = None,
    publisher: MqttPublisher | None = None,
    stage_observer: Callable[[str, str, float], None] | None = None,
    event_observer: Callable[[str, dict[str, object]], None] | None = None,
) -> IngestionJobResult:
    job_started = time.monotonic()
    name_value = job.source_stream_metadata.get(
        "title", job.source_stream_metadata.get("presentationName")
    )
    name = name_value if isinstance(name_value, str) else None
    download_timestamp: datetime | None = None
    provider_update_timestamp: datetime | None = None
    provider_to_download_s: float | None = None
    observed_marker: str | None = None
    period_candidate: PeriodEstimateCandidate | None = None
    state_update: IngestionStateUpdate | None = None
    estimated_period_band = _estimated_period_band(job.estimated_source_stream_period)

    def finish(outcome: JobOutcome, **values: Any) -> IngestionJobResult:
        if "period_estimate_candidate" not in values:
            values["period_estimate_candidate"] = period_candidate
        if "state_update" not in values:
            values["state_update"] = state_update
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
                    {
                        "measure": "download_to_job_end",
                        "duration_s": download_to_end_s,
                        "estimated_period_band": estimated_period_band,
                    },
                )
                if provider_to_download_s is not None:
                    event_observer(
                        "image_latency",
                        {
                            "measure": "provider_to_job_end",
                            "duration_s": provider_to_download_s + download_to_end_s,
                            "estimated_period_band": estimated_period_band,
                        },
                    )
        return _job_result(job, name, outcome, **values)

    try:
        reference = client.get_current_image(
            job.provider_source_stream_id,
            job.selected_rendition,
            job.source_stream_metadata,
        )
    except ProviderImageAccessError as error:
        return finish("throttled" if error.throttled else "provider_error")
    reference_marker = getattr(reference, "marker", None)
    provider_update_timestamp = getattr(reference, "provider_update_timestamp", None)
    if provider_update_timestamp is None and job.network_id == "win":
        provider_update_timestamp = _parse_provider_timestamp(reference_marker)
    # Fintraffic bulk freshness exposes only measuredTime. Its opaque image
    # marker is the ETag learned later from the full JPEG GET.
    observed_marker = reference_marker if job.network_id == "ska" else None
    if _available_freshness_unchanged(
        job,
        marker=observed_marker,
        provider_timestamp=provider_update_timestamp,
    ):
        if observed_marker is not None and event_observer is not None:
            event_observer("marker_unchanged_skip", {})
        return finish("unchanged", provider_marker=reference_marker)
    download_timestamp = datetime.now(UTC)
    period_candidate = build_period_estimate_candidate(
        job,
        provider_update_timestamp=provider_update_timestamp,
        minimum_period_seconds=minimum_period_seconds,
        direct_replacement_modulus=direct_replacement_modulus,
    )
    state_update = IngestionStateUpdate(
        source_stream_id=job.source_stream_id,
        provider_update_timestamp=provider_update_timestamp,
        provider_image_marker=observed_marker,
        download_timestamp=download_timestamp,
        period_estimate_candidate=period_candidate,
    )
    try:
        content = client.download(reference.image_url)
    except ProviderImageAccessError as error:
        downloaded_marker = getattr(client, "downloaded_marker", lambda _url: None)(
            reference.image_url
        )
        if downloaded_marker is not None:
            observed_marker = downloaded_marker
            state_update = IngestionStateUpdate(
                source_stream_id=job.source_stream_id,
                provider_update_timestamp=provider_update_timestamp,
                provider_image_marker=observed_marker,
                download_timestamp=download_timestamp,
                period_estimate_candidate=period_candidate,
            )
        return finish(
            "throttled" if error.throttled else "download_error",
            provider_marker=observed_marker or reference_marker,
        )
    downloaded_marker = getattr(client, "downloaded_marker", lambda _url: None)(
        reference.image_url
    )
    if downloaded_marker is not None:
        observed_marker = downloaded_marker
        state_update = IngestionStateUpdate(
            source_stream_id=job.source_stream_id,
            provider_update_timestamp=provider_update_timestamp,
            provider_image_marker=observed_marker,
            download_timestamp=download_timestamp,
            period_estimate_candidate=period_candidate,
        )
        if (
            job.network_id == "fin"
            and job.last_observed_image_marker == downloaded_marker
        ):
            if event_observer is not None:
                event_observer("marker_unchanged_skip", {})
            return finish("unchanged", provider_marker=downloaded_marker)
    result_marker = observed_marker if observed_marker is not None else reference_marker
    if event_observer is not None:
        event_observer(
            "source_download_bytes",
            {"size_bytes": len(content)},
        )
    try:
        source = validate_source_image(content)
    except InvalidSourceImageError:
        return finish(
            "invalid_image",
            provider_marker=result_marker,
            size_bytes=len(content),
        )
    if event_observer is not None:
        event_observer(
            "source_image",
            {
                "size_bytes": source.size_bytes,
                "width": source.width,
                "height": source.height,
                "format": source.format,
                "color_mode": source.color_mode,
                "color_depth_bits": _source_color_depth_bits(source.color_mode),
            },
        )

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
                        "estimated_period_band": estimated_period_band,
                    },
                )
    selected_transformation = transformation or TransformationConfig.from_environment()
    transformation_started = time.monotonic()
    try:
        source_provider_metadata = (
            {"resolved_target_path": reference.resolved_target_path}
            if job.network_id == "ska"
            and hasattr(reference, "resolved_target_path")
            else None
        )
        if storage is not None and publisher is not None:
            prepared = prepare_publication(
                job=job,
                source=source,
                download_timestamp=download_timestamp,
                provider_update_timestamp=provider_update_timestamp,
                source_image_provider_metadata=source_provider_metadata,
                transformation=selected_transformation,
                storage=storage,
            )
            publication = PublicationResult(
                prepared.derived,
                prepared.derived_stream_id,
                prepared.image_id,
                prepared.object_key,
                storage.reference(prepared.object_key).url,
                None,
            )
        else:
            publication = process_source_image(
                job=job,
                source=source,
                download_timestamp=download_timestamp,
                provider_update_timestamp=provider_update_timestamp,
                source_image_provider_metadata=source_provider_metadata,
                transformation=selected_transformation,
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
        return finish("transformation_error", provider_marker=result_marker)
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
                "format": publication.derived.format,
                "color_mode": publication.derived.color_mode,
                "color_depth_bits": publication.derived.color_depth,
            },
        )
        if storage is not None:
            assert publisher is not None
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
    if storage is not None and publisher is not None:
        try:
            stored = storage.upload(prepared.object_key, prepared.derived.content)
        except S3StorageError:
            return finish("storage_error", provider_marker=result_marker)
        try:
            topic = publisher.publish(
                selected_transformation.version, prepared.notification
            )
        except MqttPublicationError:
            return finish("mqtt_error", provider_marker=result_marker)
        publication = PublicationResult(
            prepared.derived,
            prepared.derived_stream_id,
            prepared.image_id,
            prepared.object_key,
            stored.url,
            topic,
        )
    if storage is not None and publisher is not None:
        state_update = IngestionStateUpdate(
            source_stream_id=job.source_stream_id,
            provider_update_timestamp=provider_update_timestamp,
            provider_image_marker=observed_marker,
            download_timestamp=download_timestamp,
            period_estimate_candidate=period_candidate,
            processed_timestamp=download_timestamp,
        )
    return finish(
        "published" if storage is not None else "downloaded",
        provider_marker=result_marker,
        size_bytes=source.size_bytes,
        width=source.width,
        height=source.height,
        image_format=source.format,
        color_mode=source.color_mode,
        period_estimate_candidate=period_candidate,
        state_update=state_update,
        derived_size_bytes=publication.derived.size_bytes,
        derived_width=publication.derived.width,
        derived_height=publication.derived.height,
        image_signature=publication.derived.image_signature,
        image_id=publication.image_id,
        object_key=publication.object_key,
        mqtt_topic=publication.mqtt_topic,
    )


def _available_freshness_unchanged(
    job: DueSourceStream,
    *,
    marker: str | None,
    provider_timestamp: datetime | None,
) -> bool:
    # Skaping's resolved-object ETag is authoritative for content freshness.
    # Last-Modified is optional timing metadata and must not force a download
    # when that ETag is unchanged.
    if job.network_id == "ska" and marker is not None:
        return marker == job.last_observed_image_marker
    comparisons: list[bool] = []
    if marker is not None:
        comparisons.append(marker == job.last_observed_image_marker)
    if provider_timestamp is not None:
        comparisons.append(provider_timestamp == job.last_observed_provider_timestamp)
    return bool(comparisons) and all(comparisons)


def _source_color_depth_bits(color_mode: str) -> int:
    return {
        "1": 1,
        "L": 8,
        "P": 8,
        "LA": 16,
        "I": 32,
        "F": 32,
        "RGB": 24,
        "YCbCr": 24,
        "HSV": 24,
        "RGBA": 32,
        "CMYK": 32,
    }.get(color_mode, 0)


def _job_result(
    job: DueSourceStream,
    name: str | None,
    outcome: JobOutcome,
    **values: Any,
) -> IngestionJobResult:
    return IngestionJobResult(
        source_stream_id=job.source_stream_id,
        country=job.country,
        name=name,
        outcome=outcome,
        **values,
    )



def _parse_provider_timestamp(marker: str | None) -> datetime | None:
    if marker is None:
        return None
    try:
        parsed = datetime.fromisoformat(marker.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None


def _estimated_period_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value <= 600:
        return "le_10m"
    if value <= 3600:
        return "10m_to_60m"
    return "gt_60m"
