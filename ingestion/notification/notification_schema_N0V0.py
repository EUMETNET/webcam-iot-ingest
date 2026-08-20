"""Build the pilot N0V0 notification payload."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from database.registry_queries import DueSourceStream
from ingestion.shared.source_image_validation import SourceImage
from ingestion.transformation.transformation_T0 import DerivedImage
from storage.s3_storage import StoredObject


SCHEMA_VERSION = "N0V0"


def build_notification(
    *,
    job: DueSourceStream,
    source: SourceImage,
    derived: DerivedImage,
    derived_stream_id: str,
    image_id: str,
    stored: StoredObject,
    download_timestamp: datetime,
    provider_update_timestamp: datetime | None,
    source_image_provider_metadata: dict[str, Any] | None = None,
    publication_timestamp: datetime | None = None,
) -> dict[str, Any]:
    published = publication_timestamp or datetime.now(UTC)
    source_stream_latitude, source_stream_longitude = _source_stream_coordinates(job)
    return {
        "schema_version": SCHEMA_VERSION,
        "image_id": image_id,
        "url": stored.url,
        "storage": {
            "type": "s3",
            "bucket": stored.bucket,
            "object_key": stored.object_key,
        },
        "network": {"network_id": job.network_id},
        "site": {
            "site_id": job.site_id,
            "latitude": job.latitude,
            "longitude": job.longitude,
            "altitude": job.altitude,
            "country": job.country,
            "corrected_latitude": job.corrected_latitude,
            "corrected_longitude": job.corrected_longitude,
            "corrected_altitude": job.corrected_altitude,
            "provider_metadata": job.site_metadata,
        },
        "source_stream": {
            "source_stream_id": job.source_stream_id,
            "selected_rendition": job.selected_rendition,
            "provider_stream_id": job.provider_source_stream_id,
            "latitude": source_stream_latitude,
            "longitude": source_stream_longitude,
            "provider_metadata": job.source_stream_metadata,
        },
        "derived_stream": {
            "derived_stream_id": derived_stream_id,
            "transformation_version": derived.transformation_version,
            "transformation_metadata": {
                "image_signature": derived.image_signature,
                "jpeg_quality": derived.jpeg_quality,
                "panoramic": derived.panoramic,
            },
        },
        "timestamps": {
            "download_timestamp": _timestamp(download_timestamp),
            "provider_update_timestamp": _timestamp(provider_update_timestamp),
            "publication_timestamp": _timestamp(published),
        },
        "source_image": {
            "width": source.width,
            "height": source.height,
            "format": source.format,
            "size_bytes": source.size_bytes,
            "colour_mode": source.color_mode,
            "provider_metadata": source_image_provider_metadata or {},
        },
        "derived_image": {
            "width": derived.width,
            "height": derived.height,
            "format": derived.format,
            "size_bytes": derived.size_bytes,
            "colour_mode": derived.color_mode,
            "colour_depth": derived.color_depth,
        },
    }


def _source_stream_coordinates(job: DueSourceStream) -> tuple[float, float]:
    latitude = job.latitude
    longitude = job.longitude
    if job.network_id == "win":
        location = job.source_stream_metadata.get("location")
        if isinstance(location, dict):
            camera_latitude = location.get("latitude")
            camera_longitude = location.get("longitude")
            if (
                isinstance(camera_latitude, (int, float))
                and not isinstance(camera_latitude, bool)
                and isinstance(camera_longitude, (int, float))
                and not isinstance(camera_longitude, bool)
            ):
                latitude = float(camera_latitude)
                longitude = float(camera_longitude)
    return latitude, longitude


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("notification timestamps must be UTC-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
