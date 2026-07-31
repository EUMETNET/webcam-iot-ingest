"""Provider-independent transformation, storage, and notification orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from config.deployment_config import TransformationConfig
from database.registry_queries import DueSourceStream
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.notification.notification_schema_N0V0 import build_notification
from ingestion.shared.derived_identifiers import (
    build_derived_stream_id,
    build_image_id,
    build_object_key,
)
from ingestion.shared.source_image_validation import SourceImage
from ingestion.transformation.transformation_T0V0 import DerivedImage, transform
from storage.s3_storage import S3Storage


@dataclass(frozen=True)
class PublicationResult:
    derived: DerivedImage
    derived_stream_id: str
    image_id: str
    object_key: str | None
    object_url: str | None
    mqtt_topic: str | None


@dataclass(frozen=True)
class PreparedPublication:
    derived: DerivedImage
    derived_stream_id: str
    image_id: str
    object_key: str
    notification: dict[str, object]


def prepare_publication(
    *,
    job: DueSourceStream,
    source: SourceImage,
    download_timestamp: datetime,
    provider_update_timestamp: datetime | None,
    transformation: TransformationConfig,
    storage: S3Storage,
    source_image_provider_metadata: dict[str, object] | None = None,
) -> PreparedPublication:
    derived = transform(source, transformation)
    derived_stream_id = build_derived_stream_id(job.source_stream_id, transformation.version)
    image_id = build_image_id(derived_stream_id, download_timestamp)
    object_key = build_object_key(
        transformation.version,
        job.network_id,
        image_id,
        download_timestamp,
        prefix=storage.prefix,
    )
    stored = storage.reference(object_key)
    notification = build_notification(
        job=job,
        source=source,
        derived=derived,
        derived_stream_id=derived_stream_id,
        image_id=image_id,
        stored=stored,
        download_timestamp=download_timestamp,
        provider_update_timestamp=provider_update_timestamp,
        source_image_provider_metadata=source_image_provider_metadata,
    )
    return PreparedPublication(
        derived, derived_stream_id, image_id, object_key, notification
    )


def process_source_image(
    *,
    job: DueSourceStream,
    source: SourceImage,
    download_timestamp: datetime,
    provider_update_timestamp: datetime | None,
    transformation: TransformationConfig,
    source_image_provider_metadata: dict[str, object] | None = None,
    storage: S3Storage | None = None,
    publisher: MqttPublisher | None = None,
) -> PublicationResult:
    if storage is None and publisher is None:
        derived = transform(source, transformation)
        derived_stream_id = build_derived_stream_id(
            job.source_stream_id, transformation.version
        )
        image_id = build_image_id(derived_stream_id, download_timestamp)
        return PublicationResult(
            derived, derived_stream_id, image_id, None, None, None
        )
    if storage is None or publisher is None:
        raise ValueError("storage and MQTT publisher must be configured together")
    prepared = prepare_publication(
        job=job,
        source=source,
        download_timestamp=download_timestamp,
        provider_update_timestamp=provider_update_timestamp,
        source_image_provider_metadata=source_image_provider_metadata,
        transformation=transformation,
        storage=storage,
    )
    stored = storage.upload(prepared.object_key, prepared.derived.content)
    topic = publisher.publish(transformation.version, prepared.notification)
    return PublicationResult(
        prepared.derived,
        prepared.derived_stream_id,
        prepared.image_id,
        prepared.object_key,
        stored.url,
        topic,
    )
