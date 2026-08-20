"""Deterministic derived-stream, image, and object-key identifiers."""

from datetime import datetime


def build_derived_stream_id(source_stream_id: str, version: str) -> str:
    if (
        not source_stream_id.isalnum()
        or not 1 <= len(version) <= 16
        or not version.isalnum()
    ):
        raise ValueError("derived stream components must be alphanumeric")
    return f"{source_stream_id}{version}"


def build_image_id(derived_stream_id: str, timestamp: datetime) -> str:
    _require_utc(timestamp)
    return f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{derived_stream_id}.jpg"


def build_object_key(
    version: str,
    network_id: str,
    image_id: str,
    timestamp: datetime,
    *,
    prefix: str = "",
) -> str:
    _require_utc(timestamp)
    parts = [
        prefix.strip("/"),
        version,
        network_id,
        timestamp.strftime("%Y"),
        timestamp.strftime("%m"),
        timestamp.strftime("%d"),
        timestamp.strftime("%H"),
        image_id,
    ]
    return "/".join(part for part in parts if part)


def _require_utc(value: datetime) -> None:
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("derived image timestamp must be UTC-aware")
