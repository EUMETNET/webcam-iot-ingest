from pathlib import Path

import pytest

from config.deployment_config import DatabaseConfig


def test_database_config_reads_password_from_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("local-test-password\n", encoding="utf-8")
    config = DatabaseConfig(
        host="localhost",
        port=5432,
        name="webcam_ingestion",
        user="webcam_ingestion",
        password_file=password_file,
        pool_size=5,
    )

    assert config.read_password() == "local-test-password"


def test_database_config_rejects_empty_password_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("\n", encoding="utf-8")
    config = DatabaseConfig(
        host="localhost",
        port=5432,
        name="webcam_ingestion",
        user="webcam_ingestion",
        password_file=password_file,
        pool_size=5,
    )

    with pytest.raises(ValueError, match="password file is empty"):
        config.read_password()
