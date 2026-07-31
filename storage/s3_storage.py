"""Immutable S3-compatible derived-image upload."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Callable
from urllib.parse import quote

import boto3
from botocore.config import Config

from config.deployment_config import S3Config


class S3StorageError(RuntimeError):
    """A derived image could not be stored after bounded attempts."""


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    url: str


class S3Storage:
    def __init__(
        self,
        config: S3Config,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        observer: Callable[[str, str, float], None] | None = None,
        event_observer: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._observer = observer
        self._event_observer = event_observer
        if client is None:
            client = create_s3_client(config)
        self._client = client

    @property
    def prefix(self) -> str:
        return self._config.prefix

    def reference(self, object_key: str) -> StoredObject:
        url = f"{self._config.public_url_base.rstrip('/')}/{quote(object_key, safe='/')}"
        return StoredObject(self._config.bucket, object_key, url)

    def upload(self, object_key: str, content: bytes) -> StoredObject:
        started = time.monotonic()
        try:
            result = self._upload(object_key, content)
        except Exception:
            if self._observer is not None:
                self._observer("s3_upload", "failure", time.monotonic() - started)
            if self._event_observer is not None:
                self._event_observer("s3_operation", {"result": "failure"})
            raise
        if self._observer is not None:
            self._observer("s3_upload", "success", time.monotonic() - started)
        if self._event_observer is not None:
            self._event_observer("s3_operation", {"result": "success"})
            self._event_observer(
                "s3_upload_bytes",
                {"size_bytes": len(content)},
            )
        return result

    def _upload(self, object_key: str, content: bytes) -> StoredObject:
        if not object_key or not content:
            raise ValueError("S3 object key and content are required")
        last_error: Exception | None = None
        digest = hashlib.sha256(content).hexdigest()
        for attempt in range(self._config.retry_count + 1):
            try:
                self._client.put_object(
                    Bucket=self._config.bucket,
                    Key=object_key,
                    Body=content,
                    ContentType="image/jpeg",
                    Metadata={"sha256": digest},
                )
                return self.reference(object_key)
            except Exception as error:  # SDK providers expose several subclasses.
                last_error = error
                if attempt < self._config.retry_count:
                    if self._event_observer is not None:
                        self._event_observer(
                            "retry",
                            {"operation": "s3_upload", "reason": "request_failure"},
                        )
                    if self._config.retry_backoff_s:
                        self._sleep(self._config.retry_backoff_s * (2**attempt))
        raise S3StorageError("S3 derived-image upload failed") from last_error


def create_s3_client(config: S3Config) -> Any:
    """Create the configured authenticated client without exposing credentials."""
    access_key, secret_key = config.read_credentials()
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": config.addressing_style},
            retries={"max_attempts": 0, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        ),
    )
