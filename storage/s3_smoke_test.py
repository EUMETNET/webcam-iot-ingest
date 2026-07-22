"""Measure one bounded S3 upload/download and remove the temporary object."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid

from config.deployment_config import S3Config
from storage.s3_storage import create_s3_client


def run(size_mib: int) -> dict[str, object]:
    if not 1 <= size_mib <= 64:
        raise ValueError("size-mib must be between 1 and 64")
    config = S3Config.from_environment()
    client = create_s3_client(config)
    payload = os.urandom(size_mib * 1024 * 1024)
    expected_digest = hashlib.sha256(payload).digest()
    object_key = f"{config.prefix + '/' if config.prefix else ''}_smoke_test/{uuid.uuid4().hex}.bin"
    cleanup_succeeded = False
    try:
        started = time.perf_counter()
        client.put_object(
            Bucket=config.bucket,
            Key=object_key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        upload_seconds = time.perf_counter() - started

        started = time.perf_counter()
        response = client.get_object(Bucket=config.bucket, Key=object_key)
        downloaded = response["Body"].read()
        download_seconds = time.perf_counter() - started
        verified = hashlib.sha256(downloaded).digest() == expected_digest
    finally:
        try:
            client.delete_object(Bucket=config.bucket, Key=object_key)
            cleanup_succeeded = True
        except Exception:
            cleanup_succeeded = False
    size_mb = len(payload) / 1_000_000
    return {
        "bucket": config.bucket,
        "size_bytes": len(payload),
        "upload_seconds": round(upload_seconds, 4),
        "upload_MB_s": round(size_mb / upload_seconds, 2),
        "download_seconds": round(download_seconds, 4),
        "download_MB_s": round(size_mb / download_seconds, 2),
        "content_verified": verified,
        "temporary_object_removed": cleanup_succeeded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=16)
    args = parser.parse_args()
    print(json.dumps(run(args.size_mib), sort_keys=True))


if __name__ == "__main__":
    main()
