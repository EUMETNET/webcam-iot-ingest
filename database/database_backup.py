"""Create a full timestamped PostgreSQL dump and store it in S3."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from botocore.exceptions import ClientError

from config.deployment_config import DatabaseConfig, S3Config
from database.database_backup_cleanup import cleanup_database_backups
from storage.batch_metrics import BatchJobMetrics
from storage.s3_storage import create_s3_client


@dataclass
class BackupResult:
    dry_run: bool
    object_key: str
    size_bytes: int = 0
    sha256: str | None = None
    duration_seconds: float = 0.0
    dump_duration_seconds: float = 0.0
    upload_duration_seconds: float = 0.0
    metrics_published: bool = False
    cleanup_deleted: int = 0
    cleanup_deleted_bytes: int = 0


def backup_object_key(timestamp: datetime, prefix: str) -> str:
    timestamp = timestamp.astimezone(timezone.utc)
    base = prefix.strip("/")
    suffix = (
        f"{timestamp:%Y/%m/%d}/"
        f"webcam_ingestion_{timestamp:%Y%m%dT%H%M%SZ}.dump"
    )
    return f"{base}/{suffix}" if base else suffix


def create_database_backup(
    *,
    database: DatabaseConfig,
    storage: S3Config,
    dry_run: bool = False,
    timestamp: datetime | None = None,
    backup_prefix: str = "backups/postgresql",
    pg_dump_binary: str = "pg_dump",
    pg_dump_mode: str = "direct",
    client: Any | None = None,
    metrics: BatchJobMetrics | None = None,
) -> BackupResult:
    timestamp = timestamp or datetime.now(timezone.utc)
    key = backup_object_key(timestamp, backup_prefix)
    result = BackupResult(dry_run=dry_run, object_key=key)
    if dry_run:
        return result

    metrics = metrics or BatchJobMetrics.from_environment("database_backup")
    started = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(
            prefix="webcam-ingestion-", suffix=".dump", dir="/tmp"
        ) as temporary:
            dump_started = time.monotonic()
            if pg_dump_mode == "direct":
                environment = os.environ.copy()
                environment["PGPASSWORD"] = database.read_password()
                command = [
                    pg_dump_binary,
                    "--format=custom",
                    "--no-password",
                    "--host",
                    database.host,
                    "--port",
                    str(database.port),
                    "--username",
                    database.user,
                    "--dbname",
                    database.name,
                    "--file",
                    temporary.name,
                ]
                subprocess.run(
                    command, check=True, env=environment, capture_output=True
                )
            elif pg_dump_mode == "docker-compose":
                command = [
                    "docker",
                    "compose",
                    "--env-file",
                    ".env",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "--format=custom",
                    "--username",
                    database.user,
                    "--dbname",
                    database.name,
                ]
                with open(temporary.name, "wb") as output:
                    subprocess.run(
                        command,
                        check=True,
                        stdout=output,
                        stderr=subprocess.PIPE,
                    )
            else:
                raise ValueError("pg_dump mode must be direct or docker-compose")
            result.dump_duration_seconds = time.monotonic() - dump_started
            content = Path(temporary.name).read_bytes()
            result.size_bytes = len(content)
            result.sha256 = hashlib.sha256(content).hexdigest()

            upload_started = time.monotonic()
            s3 = client or create_s3_client(storage)
            try:
                s3.head_object(Bucket=storage.bucket, Key=key)
            except ClientError as error:
                code = str(error.response.get("Error", {}).get("Code", ""))
                status = error.response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode"
                )
                if code not in {"404", "NoSuchKey", "NotFound"} and status != 404:
                    raise
            else:
                raise RuntimeError("database backup object key already exists")
            s3.put_object(
                Bucket=storage.bucket,
                Key=key,
                Body=content,
                ContentType="application/x-postgresql-custom-dump",
                Metadata={"sha256": result.sha256},
            )
            stored = s3.head_object(Bucket=storage.bucket, Key=key)
            if (
                int(stored.get("ContentLength", -1)) != result.size_bytes
                or stored.get("Metadata", {}).get("sha256") != result.sha256
            ):
                raise RuntimeError("stored database backup verification failed")
            cleanup = cleanup_database_backups(
                config=storage,
                current_key=key,
                backup_prefix=backup_prefix,
                client=s3,
            )
            result.cleanup_deleted = cleanup.deleted
            result.cleanup_deleted_bytes = cleanup.deleted_bytes
            result.upload_duration_seconds = time.monotonic() - upload_started
    except Exception:
        result.duration_seconds = time.monotonic() - started
        metrics.publish(
            success=False,
            duration_s=result.duration_seconds,
            bytes_by_outcome={"dump": result.size_bytes},
            stages={
                "dump": result.dump_duration_seconds,
                "upload": result.upload_duration_seconds,
            },
        )
        raise

    result.duration_seconds = time.monotonic() - started
    result.metrics_published = metrics.publish(
        success=True,
        duration_s=result.duration_seconds,
        items={"stored": 1},
        bytes_by_outcome={
            "dump": result.size_bytes,
            "stored": result.size_bytes,
        },
        stages={
            "dump": result.dump_duration_seconds,
            "upload": result.upload_duration_seconds,
        },
        backup_size_bytes=result.size_bytes,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and print the future key without dumping",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("DATABASE_BACKUP_S3_PREFIX", "backups/postgresql"),
    )
    parser.add_argument(
        "--pg-dump-binary",
        default=os.getenv("PG_DUMP_BINARY", "pg_dump"),
    )
    parser.add_argument(
        "--pg-dump-mode",
        choices=("direct", "docker-compose"),
        default=os.getenv("PG_DUMP_MODE", "direct"),
    )
    args = parser.parse_args()
    result = create_database_backup(
        database=DatabaseConfig.from_environment(),
        storage=S3Config.from_environment(),
        dry_run=args.dry_run,
        backup_prefix=args.prefix,
        pg_dump_binary=args.pg_dump_binary,
        pg_dump_mode=args.pg_dump_mode,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
