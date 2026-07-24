from datetime import datetime, timezone
from pathlib import Path

from database.database_backup import backup_object_key, create_database_backup


def test_backup_key_is_outside_image_spool_prefixes():
    assert backup_object_key(
        datetime(2026, 7, 24, 15, 1, 2, tzinfo=timezone.utc),
        "backups/postgresql",
    ) == "backups/postgresql/2026/07/24/webcam_ingestion_20260724T150102Z.dump"


def test_backup_dry_run_does_not_read_secrets_or_execute_dump(tmp_path: Path):
    class Config:
        pass

    result = create_database_backup(
        database=Config(),
        storage=Config(),
        dry_run=True,
        timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    assert result.object_key.endswith("webcam_ingestion_20260724T000000Z.dump")
    assert result.size_bytes == 0
