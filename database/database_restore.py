"""List, verify, or explicitly restore a PostgreSQL dump stored in S3."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any

import psycopg

from config.deployment_config import DatabaseConfig, S3Config
from observability.maintenance_metrics import MaintenanceJobMetrics
from storage.s3_storage import create_s3_client


_BACKUP_KEY = re.compile(
    r"^(?P<prefix>.+)/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"webcam_ingestion_(?P=year)(?P=month)(?P=day)T\d{6}Z\.dump$"
)


@dataclass(frozen=True)
class BackupObject:
    object_key: str
    size_bytes: int
    last_modified: str | None


@dataclass
class RestoreResult:
    object_key: str
    target_database: str
    dry_run: bool
    size_bytes: int = 0
    sha256: str | None = None
    verified: bool = False
    restored: bool = False
    duration_seconds: float = 0.0
    download_duration_seconds: float = 0.0
    validation_duration_seconds: float = 0.0
    restore_duration_seconds: float = 0.0
    metrics_published: bool = False


def _canonical_backup_key(object_key: str, backup_prefix: str) -> bool:
    match = _BACKUP_KEY.fullmatch(object_key)
    return match is not None and match.group("prefix") == backup_prefix.strip("/")


def list_database_backups(
    *,
    storage: S3Config,
    backup_prefix: str = "backups/postgresql",
    client: Any | None = None,
) -> list[BackupObject]:
    s3 = client or create_s3_client(storage)
    prefix = f"{backup_prefix.strip('/')}/"
    paginator = s3.get_paginator("list_objects_v2")
    objects: list[BackupObject] = []
    for page in paginator.paginate(Bucket=storage.bucket, Prefix=prefix):
        for item in page.get("Contents", ()):
            key = str(item.get("Key", ""))
            if not _canonical_backup_key(key, backup_prefix):
                continue
            modified = item.get("LastModified")
            objects.append(
                BackupObject(
                    object_key=key,
                    size_bytes=int(item.get("Size", 0)),
                    last_modified=(modified.isoformat() if modified else None),
                )
            )
    return sorted(objects, key=lambda item: item.object_key, reverse=True)


def _download_verified_dump(
    *,
    storage: S3Config,
    object_key: str,
    destination: Path,
    client: Any,
) -> tuple[int, str]:
    if not _canonical_backup_key(object_key, "backups/postgresql"):
        raise ValueError("restore object key is not a canonical PostgreSQL backup")
    head = client.head_object(Bucket=storage.bucket, Key=object_key)
    expected_size = int(head.get("ContentLength", -1))
    expected_sha = str(head.get("Metadata", {}).get("sha256", "")).lower()
    if expected_size < 1:
        raise RuntimeError("database backup is empty")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RuntimeError("database backup has no valid SHA-256 metadata")
    response = client.get_object(Bucket=storage.bucket, Key=object_key)
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as output:
        body = response["Body"]
        while chunk := body.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    actual_sha = digest.hexdigest()
    if size != expected_size:
        raise RuntimeError("downloaded database backup size does not match S3 metadata")
    if actual_sha != expected_sha:
        raise RuntimeError("downloaded database backup SHA-256 does not match S3 metadata")
    return size, actual_sha


def _validate_restored_database(
    database: DatabaseConfig, target_database: str
) -> None:
    with psycopg.connect(
        host=database.host,
        port=database.port,
        dbname=target_database,
        user=database.user,
        password=database.read_password(),
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT to_regclass('public.network'),
                       to_regclass('public.site'),
                       to_regclass('public.source_stream')
                """
            )
            if cursor.fetchone() != ("network", "site", "source_stream"):
                raise RuntimeError("restored database is missing pilot registry tables")
            cursor.execute(
                "SELECT count(*) FROM source_stream WHERE source_stream_id IS NULL"
            )
            if cursor.fetchone()[0] != 0:
                raise RuntimeError("restored source-stream registry failed validation")


def restore_database_backup(
    *,
    database: DatabaseConfig,
    storage: S3Config,
    object_key: str,
    dry_run: bool = False,
    target_database: str | None = None,
    pg_restore_binary: str = "pg_restore",
    client: Any | None = None,
    metrics: MaintenanceJobMetrics | None = None,
) -> RestoreResult:
    target = target_database or database.name
    result = RestoreResult(
        object_key=object_key,
        target_database=target,
        dry_run=dry_run,
    )
    metrics = metrics or MaintenanceJobMetrics.from_environment("database_restore")
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="webcam-db-restore-") as directory:
            dump_path = Path(directory) / "database.dump"
            download_started = time.monotonic()
            s3 = client or create_s3_client(storage)
            result.size_bytes, result.sha256 = _download_verified_dump(
                storage=storage,
                object_key=object_key,
                destination=dump_path,
                client=s3,
            )
            result.download_duration_seconds = time.monotonic() - download_started

            environment = os.environ.copy()
            environment["PGPASSWORD"] = database.read_password()
            validation_started = time.monotonic()
            subprocess.run(
                [pg_restore_binary, "--list", str(dump_path)],
                check=True,
                env=environment,
                capture_output=True,
            )
            result.validation_duration_seconds = (
                time.monotonic() - validation_started
            )
            result.verified = True

            if not dry_run:
                restore_started = time.monotonic()
                subprocess.run(
                    [
                        pg_restore_binary,
                        "--clean",
                        "--if-exists",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--host",
                        database.host,
                        "--port",
                        str(database.port),
                        "--username",
                        database.user,
                        "--dbname",
                        target,
                        str(dump_path),
                    ],
                    check=True,
                    env=environment,
                    capture_output=True,
                )
                result.restore_duration_seconds = time.monotonic() - restore_started
                _validate_restored_database(database, target)
                result.restored = True
    except Exception:
        result.duration_seconds = time.monotonic() - started
        metrics.publish(
            success=False,
            dry_run=dry_run,
            duration_s=result.duration_seconds,
            items={"failed": 1},
            bytes_by_outcome={"downloaded": result.size_bytes},
            stages={
                "download": result.download_duration_seconds,
                "validation": result.validation_duration_seconds,
                "restore": result.restore_duration_seconds,
            },
        )
        raise
    result.duration_seconds = time.monotonic() - started
    result.metrics_published = metrics.publish(
        success=True,
        dry_run=dry_run,
        duration_s=result.duration_seconds,
        items={"verified": 1, "restored": int(result.restored)},
        bytes_by_outcome={"downloaded": result.size_bytes},
        stages={
            "download": result.download_duration_seconds,
            "validation": result.validation_duration_seconds,
            "restore": result.restore_duration_seconds,
        },
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list canonical backups")
    parser.add_argument("--object-key")
    parser.add_argument("--confirm-object-key")
    parser.add_argument("--target-database")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    storage = S3Config.from_environment()
    if args.list:
        if args.object_key or args.confirm_object_key or args.target_database:
            parser.error("--list cannot be combined with restore arguments")
        print(
            json.dumps(
                [asdict(item) for item in list_database_backups(storage=storage)],
                sort_keys=True,
            )
        )
        return
    if not args.object_key:
        parser.error("--object-key is required unless --list is used")
    if not args.dry_run and args.confirm_object_key != args.object_key:
        parser.error("a live restore requires matching --confirm-object-key")
    result = restore_database_backup(
        database=DatabaseConfig.from_environment(),
        storage=storage,
        object_key=args.object_key,
        target_database=args.target_database,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
