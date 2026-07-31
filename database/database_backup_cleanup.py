"""Conservatively remove superseded daily PostgreSQL dumps from S3."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
import re
import time
from typing import Any, Iterator

from config.deployment_config import S3Config
from storage.batch_metrics import BatchJobMetrics
from storage.s3_storage import create_s3_client


_DUMP_NAME = re.compile(
    r"^webcam_ingestion_(?P<timestamp>\d{8}T\d{6}Z)\.dump$"
)


@dataclass
class BackupCleanupResult:
    dry_run: bool
    current_key: str
    examined: int = 0
    eligible: int = 0
    deleted: int = 0
    failed: int = 0
    skipped_unknown: int = 0
    deleted_bytes: int = 0
    duration_seconds: float = 0.0
    metrics_published: bool = False
    selected_keys: list[str] | None = None


def _backup_date(key: str, prefix: str) -> date | None:
    root = prefix.strip("/")
    expected = f"{root}/" if root else ""
    if not key.startswith(expected):
        return None
    parts = key[len(expected) :].split("/")
    if len(parts) != 4:
        return None
    year, month, day, filename = parts
    match = _DUMP_NAME.fullmatch(filename)
    if match is None:
        return None
    try:
        path_date = date(int(year), int(month), int(day))
        filename_date = datetime.strptime(
            match.group("timestamp"), "%Y%m%dT%H%M%SZ"
        ).date()
    except ValueError:
        return None
    return path_date if path_date == filename_date else None


def _objects(client: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket, "Prefix": f"{prefix.strip('/')}/"}
    for page in paginator.paginate(**kwargs):
        yield from page.get("Contents", ())


def cleanup_database_backups(
    *,
    config: S3Config,
    current_key: str,
    backup_prefix: str = "backups/postgresql",
    dry_run: bool = False,
    client: Any | None = None,
    metrics: BatchJobMetrics | None = None,
    include_keys: bool = False,
) -> BackupCleanupResult:
    current_date = _backup_date(current_key, backup_prefix)
    if current_date is None:
        raise ValueError("current backup key is not canonical")
    result = BackupCleanupResult(
        dry_run=dry_run,
        current_key=current_key,
        selected_keys=[] if include_keys else None,
    )
    client = client or create_s3_client(config)
    metrics = metrics or BatchJobMetrics.from_environment("database_backup_cleanup")
    started = time.monotonic()
    recognized: list[tuple[str, date, int]] = []
    current_seen = False
    try:
        for item in _objects(client, config.bucket, backup_prefix):
            result.examined += 1
            key = str(item["Key"])
            backup_date = _backup_date(key, backup_prefix)
            if backup_date is None:
                result.skipped_unknown += 1
                continue
            current_seen = current_seen or key == current_key
            recognized.append((key, backup_date, int(item.get("Size", 0))))
        if not current_seen:
            raise RuntimeError("verified current backup is absent from S3 listing")

        preceding_dates = {
            backup_date
            for _, backup_date, _ in recognized
            if backup_date < current_date
            and (backup_date.year, backup_date.month)
            == (current_date.year, current_date.month)
        }
        selected_date = max(preceding_dates) if preceding_dates else None
        candidates = [
            (key, size)
            for key, backup_date, size in recognized
            if backup_date == selected_date
        ]
        result.eligible = len(candidates)
        if result.selected_keys is not None:
            result.selected_keys.extend(key for key, _ in candidates)
        if not dry_run and candidates:
            response = client.delete_objects(
                Bucket=config.bucket,
                Delete={
                    "Objects": [{"Key": key} for key, _ in candidates],
                    "Quiet": False,
                },
            )
            deleted_keys = {row["Key"] for row in response.get("Deleted", ())}
            error_keys = {row["Key"] for row in response.get("Errors", ())}
            sizes = dict(candidates)
            result.deleted = len(deleted_keys)
            result.deleted_bytes = sum(sizes[key] for key in deleted_keys)
            result.failed = len(error_keys) + max(
                0, len(candidates) - len(deleted_keys) - len(error_keys)
            )
    except Exception:
        result.duration_seconds = time.monotonic() - started
        metrics.publish(
            success=False,
            dry_run=dry_run,
            duration_s=result.duration_seconds,
            items={"examined": result.examined, "failed": result.failed + 1},
        )
        raise
    result.duration_seconds = time.monotonic() - started
    result.metrics_published = metrics.publish(
        success=result.failed == 0,
        dry_run=dry_run,
        duration_s=result.duration_seconds,
        items={
            "examined": result.examined,
            "eligible": result.eligible,
            "deleted": result.deleted,
            "failed": result.failed,
            "skipped_unknown": result.skipped_unknown,
        },
        bytes_by_outcome={"deleted": result.deleted_bytes},
    )
    if result.failed:
        raise RuntimeError(f"{result.failed} database backups could not be deleted")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-key", required=True)
    parser.add_argument(
        "--prefix", default="backups/postgresql"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-keys", action="store_true")
    args = parser.parse_args()
    result = cleanup_database_backups(
        config=S3Config.from_environment(),
        current_key=args.current_key,
        backup_prefix=args.prefix,
        dry_run=args.dry_run,
        include_keys=args.show_keys,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
