from pathlib import Path
import hashlib
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from config.deployment_config import S3Config
from storage.s3_storage import S3Storage, S3StorageError


def config(retries: int = 1) -> S3Config:
    return S3Config(
        endpoint_url="https://objects.example",
        bucket="webcam",
        prefix="",
        public_url_base="https://public.example/webcam",
        region=None,
        access_key_file=Path("unused"),
        secret_key_file=Path("unused"),
        retry_count=retries,
        retry_backoff_s=0,
    )


def test_uploads_immutable_jpeg_and_returns_reference() -> None:
    client = Mock()
    events = []
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )
    stored = S3Storage(
        config(),
        client=client,
        event_observer=lambda event, values: events.append((event, values)),
    ).upload("T0V0/win/a b.jpg", b"jpeg")

    client.put_object.assert_called_once_with(
        Bucket="webcam",
        Key="T0V0/win/a b.jpg",
        Body=b"jpeg",
        ContentType="image/jpeg",
        Metadata={"sha256": hashlib.sha256(b"jpeg").hexdigest()},
    )
    assert stored.url == "https://public.example/webcam/T0V0/win/a%20b.jpg"
    assert events == [
        ("s3_operation", {"result": "success"}),
        ("s3_upload_bytes", {"size_bytes": 4}),
    ]


def test_retries_once_then_reports_sanitized_failure() -> None:
    client = Mock()
    client.put_object.side_effect = RuntimeError("secret response")

    events = []
    with pytest.raises(S3StorageError) as caught:
        S3Storage(
            config(),
            client=client,
            event_observer=lambda event, values: events.append((event, values)),
        ).upload("key", b"jpeg")

    assert client.put_object.call_count == 2
    assert events == [
        ("retry", {"operation": "s3_upload", "reason": "request_failure"}),
        ("s3_operation", {"result": "failure"}),
    ]
    assert "secret response" not in str(caught.value)


def test_upload_does_not_issue_a_preliminary_head_request() -> None:
    client = Mock()
    events = []
    content = b"jpeg"
    S3Storage(
        config(),
        client=client,
        event_observer=lambda event, values: events.append((event, values)),
    ).upload("key", content)

    client.head_object.assert_not_called()
    client.put_object.assert_called_once()
    assert ("s3_operation", {"result": "success"}) in events
