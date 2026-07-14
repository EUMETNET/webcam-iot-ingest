"""Deployment configuration loaded without embedding secret values."""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password_file: Path
    pool_size: int

    @classmethod
    def from_environment(cls) -> "DatabaseConfig":
        return cls(
            host=os.getenv("DATABASE_HOST", "localhost"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            name=os.getenv("DATABASE_NAME", "webcam_ingestion"),
            user=os.getenv("DATABASE_USER", "webcam_ingestion"),
            password_file=Path(
                os.getenv("DATABASE_PASSWORD_FILE", ".secrets/database_password")
            ),
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "5")),
        )

    def read_password(self) -> str:
        password = self.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError(f"Database password file is empty: {self.password_file}")
        return password
