"""Delete canonical derived images older than a configured exact age."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import time
from typing import Any, Iterator

from config.deployment_config import S3Config
from storage.batch_metrics import BatchJobMetrics
from storage.s3_storage import create_s3_client


_IMAGE_KEY = re.compile(
    r"^(?:(?P<prefix>.+)/)?"
    r"(?P<version>[A-Za-z0-9]{4})/(?P<network>win|fin|ska)/"
    r"(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/(?P<hour>\d{2})/"
    r"(?P<timestamp>\d{8}T\d{6}Z)_[^/]+\.jpg$"
)


@dataclass
class CleanupResult:
    dry_run: bool
    retention_hours: float
    cutoff_timestamp: str
    examined: int = 0
    eligible: int = 0
    deleted: int = 0
    failed: int = 0
    skipped_unknown: int = 0
    eligible_bytes: int = 0
    deleted_bytes: int = 0
    duration_seconds: float = 0.0
    metrics_published: bool = False
    selected_keys: list[str] | None = None


def image_download_timestamp(
    object_key: str,
    configured_prefix: str,
    *,
    transformation_prefix: str | None = None,
) -> datetime | None:
    match = _IMAGE_KEY.fullmatch(object_key)
    if match is None:
        return None
    actual_prefix = match.group("prefix") or ""
    if actual_prefix != configured_prefix.strip("/"):
        return None
    if (
        transformation_prefix is not None
        and match.group("version") != transformation_prefix
    ):
        return None
    try:
        timestamp = datetime.strptime(
            match.group("timestamp"), "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=timezone.utc)
        path_timestamp = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    if timestamp.replace(minute=0, second=0) != path_timestamp:
        return None
    return timestamp


def _objects(client: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = f"{prefix.strip('/')}/"
    for page in paginator.paginate(**kwargs):
        yield from page.get("Contents", ())


def cleanup_spool(
    *,
    config: S3Config,
    older_than_hours: float,
    dry_run: bool,
    limit: int | None = None,
    now: datetime | None = None,
    client: Any | None = None,
    metrics: BatchJobMetrics | None = None,
    include_keys: bool = False,
    transformation_prefix: str = "T0V0",
    all_transformation_prefixes: bool = False,
) -> CleanupResult:
    if older_than_hours <= 0:
        raise ValueError("retention must be positive")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not all_transformation_prefixes and not re.fullmatch(
        r"[A-Za-z0-9]{4}", transformation_prefix
    ):
        raise ValueError("transformation prefix must contain four alphanumerics")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=older_than_hours)
    result = CleanupResult(
        dry_run=dry_run,
        retention_hours=older_than_hours,
        cutoff_timestamp=cutoff.isoformat(),
        selected_keys=[] if include_keys else None,
    )
    client = client or create_s3_client(config)
    metrics = metrics or BatchJobMetrics.from_environment("spool_cleanup")
    started = time.monotonic()
    listing_started = time.monotonic()
    candidates: list[tuple[str, int]] = []
    try:
        listing_prefix = config.prefix
        if not all_transformation_prefixes:
            listing_prefix = "/".join(
                part
                for part in (config.prefix.strip("/"), transformation_prefix)
                if part
            )
        for item in _objects(client, config.bucket, listing_prefix):
            result.examined += 1
            key = str(item["Key"])
            timestamp = image_download_timestamp(
                key,
                config.prefix,
                transformation_prefix=(
                    None if all_transformation_prefixes else transformation_prefix
                ),
            )
            if timestamp is None:
                result.skipped_unknown += 1
                continue
            if timestamp < cutoff:
                size = int(item.get("Size", 0))
                candidates.append((key, size))
                if result.selected_keys is not None:
                    result.selected_keys.append(key)
                result.eligible += 1
                result.eligible_bytes += size
                if limit is not None and len(candidates) >= limit:
                    break
        listing_duration = time.monotonic() - listing_started
        deletion_started = time.monotonic()
        if not dry_run:
            for offset in range(0, len(candidates), 1000):
                batch = candidates[offset : offset + 1000]
                response = client.delete_objects(
                    Bucket=config.bucket,
                    Delete={
                        "Objects": [{"Key": key} for key, _ in batch],
                        "Quiet": False,
                    },
                )
                deleted_keys = {row["Key"] for row in response.get("Deleted", ())}
                error_keys = {row["Key"] for row in response.get("Errors", ())}
                result.deleted += len(deleted_keys)
                result.failed += len(error_keys)
                sizes = dict(batch)
                result.deleted_bytes += sum(sizes[key] for key in deleted_keys)
                unreported = len(batch) - len(deleted_keys) - len(error_keys)
                result.failed += max(0, unreported)
        deletion_duration = time.monotonic() - deletion_started
    except Exception:
        result.duration_seconds = time.monotonic() - started
        metrics.publish(
            success=False,
            duration_s=result.duration_seconds,
            items={"examined": result.examined, "failed": result.failed + 1},
            bytes_by_outcome={"eligible": result.eligible_bytes},
        )
        raise
    result.duration_seconds = time.monotonic() - started
    success = result.failed == 0
    result.metrics_published = metrics.publish(
        success=success,
        dry_run=dry_run,
        duration_s=result.duration_seconds,
        items={
            "examined": result.examined,
            "eligible": result.eligible,
            "deleted": result.deleted,
            "failed": result.failed,
            "skipped_unknown": result.skipped_unknown,
        },
        bytes_by_outcome={
            "eligible": result.eligible_bytes,
            "deleted": result.deleted_bytes,
        },
        stages={"listing": listing_duration, "deletion": deletion_duration},
        retention_hours=older_than_hours,
    )
    if not success:
        raise RuntimeError(f"{result.failed} S3 objects could not be deleted")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-hours", type=float, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--transformation-prefix",
        default="T0V0",
        help="four-character transformation prefix cleaned by default",
    )
    parser.add_argument(
        "--all-transformation-prefixes",
        action="store_true",
        help="clean every recognized transformation prefix",
    )
    parser.add_argument(
        "--show-keys",
        action="store_true",
        help="include eligible keys in output; requires --limit",
    )
    args = parser.parse_args()
    if args.show_keys and args.limit is None:
        parser.error("--show-keys requires --limit")
    result = cleanup_spool(
        config=S3Config.from_environment(),
        older_than_hours=args.older_than_hours,
        dry_run=args.dry_run,
        limit=args.limit,
        include_keys=args.show_keys,
        transformation_prefix=args.transformation_prefix,
        all_transformation_prefixes=args.all_transformation_prefixes,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
