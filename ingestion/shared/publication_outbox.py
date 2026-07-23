"""Durable S3/MQTT publication delivery and replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg

from database.registry_queries import (
    PendingPublication,
    complete_publication,
    get_pending_publications,
    mark_publication_uploaded,
    record_publication_failure,
)
from ingestion.notification.mqtt_publisher import MqttPublicationError, MqttPublisher
from storage.s3_storage import S3Storage, S3StorageError


DeliveryOutcome = Literal["published", "storage_error", "mqtt_error"]


@dataclass(frozen=True)
class DeliveryResult:
    image_id: str
    outcome: DeliveryOutcome


def drain_publication_outbox(
    connection: psycopg.Connection,
    storage: S3Storage,
    publisher: MqttPublisher,
    *,
    limit: int,
    network_id: str | None = None,
) -> list[DeliveryResult]:
    pending = get_pending_publications(
        connection, limit=limit, network_id=network_id
    )
    return [deliver_publication(connection, storage, publisher, item) for item in pending]


def deliver_publication(
    connection: psycopg.Connection,
    storage: S3Storage,
    publisher: MqttPublisher,
    item: PendingPublication,
) -> DeliveryResult:
    if item.stage == "pending_s3":
        try:
            storage.upload(item.object_key, item.derived_content)
        except S3StorageError:
            record_publication_failure(connection, item.image_id, "s3_upload")
            connection.commit()
            return DeliveryResult(item.image_id, "storage_error")
        mark_publication_uploaded(connection, item.image_id)
        connection.commit()
    version = str(
        item.notification.get("derived_stream", {}).get(
            "transformation_version", ""
        )
    )
    try:
        publisher.publish(version, item.notification)
    except MqttPublicationError:
        record_publication_failure(connection, item.image_id, "mqtt_publish")
        connection.commit()
        return DeliveryResult(item.image_id, "mqtt_error")
    complete_publication(connection, item.image_id)
    connection.commit()
    return DeliveryResult(item.image_id, "published")
