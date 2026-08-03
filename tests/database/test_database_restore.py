from datetime import datetime, timezone
from io import BytesIO
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from config.deployment_config import DatabaseConfig, S3Config
from database.database_restore import (
    list_database_backups,
    restore_database_backup,
)


class Metrics:
    def publish(self, **kwargs):
        self.kwargs = kwargs
        return True


class Paginator:
    def paginate(self, **kwargs):
        assert kwargs["Prefix"] == "backups/postgresql/"
        return [
            {
                "Contents": [
                    {
                        "Key": "backups/postgresql/2026/08/02/webcam_ingestion_20260802T120000Z.dump",
                        "Size": 123,
                        "LastModified": datetime(2026, 8, 2, tzinfo=timezone.utc),
                    },
                    {"Key": "backups/postgresql/unexpected.dump", "Size": 1},
                ]
            }
        ]


class Client:
    def __init__(self, content: bytes):
        self.content = content

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return Paginator()

    def head_object(self, **kwargs):
        return {
            "ContentLength": len(self.content),
            "Metadata": {"sha256": hashlib.sha256(self.content).hexdigest()},
        }

    def get_object(self, **kwargs):
        return {"Body": BytesIO(self.content)}


def storage() -> S3Config:
    return S3Config(
        endpoint_url="https://s3.example",
        bucket="bucket",
        prefix="",
        public_url_base="https://s3.example/bucket",
        region=None,
        access_key_file=None,
        secret_key_file=None,
        retry_count=0,
        retry_backoff_s=0,
    )


def database(password_file: Path) -> DatabaseConfig:
    password_file.write_text("secret", encoding="utf-8")
    return DatabaseConfig(
        host="postgres",
        port=5432,
        name="webcam_ingestion",
        user="webcam_ingestion",
        password_file=password_file,
        pool_size=1,
    )


def test_list_database_backups_excludes_unexpected_keys():
    backups = list_database_backups(
        storage=storage(), client=Client(b"dump")
    )
    assert len(backups) == 1
    assert backups[0].object_key.endswith("20260802T120000Z.dump")


def test_dry_run_downloads_checksums_and_validates_without_restore(tmp_path: Path):
    metrics = Metrics()
    with patch("database.database_restore.subprocess.run") as run:
        result = restore_database_backup(
            database=database(tmp_path / "password"),
            storage=storage(),
            object_key="backups/postgresql/2026/08/02/webcam_ingestion_20260802T120000Z.dump",
            dry_run=True,
            client=Client(b"custom dump"),
            metrics=metrics,
        )

    assert result.verified is True
    assert result.restored is False
    assert run.call_count == 1
    assert "--list" in run.call_args.args[0]
    assert metrics.kwargs["dry_run"] is True


def test_checksum_mismatch_prevents_validation_and_restore(tmp_path: Path):
    client = Client(b"custom dump")
    client.head_object = lambda **kwargs: {
        "ContentLength": len(client.content),
        "Metadata": {"sha256": "0" * 64},
    }
    with patch("database.database_restore.subprocess.run") as run:
        with pytest.raises(RuntimeError, match="SHA-256"):
            restore_database_backup(
                database=database(tmp_path / "password"),
                storage=storage(),
                object_key="backups/postgresql/2026/08/02/webcam_ingestion_20260802T120000Z.dump",
                dry_run=True,
                client=client,
                metrics=Metrics(),
            )
    run.assert_not_called()


def test_noncanonical_restore_key_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="canonical"):
        restore_database_backup(
            database=database(tmp_path / "password"),
            storage=storage(),
            object_key="T0V0/win/image.jpg",
            dry_run=True,
            client=Client(b"dump"),
            metrics=Metrics(),
        )
