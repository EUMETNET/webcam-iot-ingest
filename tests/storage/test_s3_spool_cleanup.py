from datetime import datetime, timezone

from config.deployment_config import S3Config
from storage.s3_spool_cleanup import cleanup_spool, image_download_timestamp


class Metrics:
    def publish(self, **kwargs):
        self.kwargs = kwargs
        return True


class Paginator:
    def paginate(self, **kwargs):
        return [
            {
                "Contents": [
                    {
                        "Key": "T0V0/win/2026/07/23/10/20260723T103000Z_win1T0V0.jpg",
                        "Size": 100,
                    },
                    {
                        "Key": "T0V0/fin/2026/07/24/11/20260724T110000Z_fin1T0V0.jpg",
                        "Size": 200,
                    },
                    {"Key": "backups/postgresql/database.dump", "Size": 999},
                ]
            }
        ]


class Client:
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return Paginator()

    def delete_objects(self, **kwargs):
        return {"Deleted": kwargs["Delete"]["Objects"]}


def config():
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


def test_only_canonical_image_keys_have_download_timestamps():
    assert image_download_timestamp(
        "T0V0/win/2026/07/23/10/20260723T103000Z_win1T0V0.jpg", ""
    ) == datetime(2026, 7, 23, 10, 30, tzinfo=timezone.utc)
    assert image_download_timestamp("backups/postgresql/database.dump", "") is None
    assert (
        image_download_timestamp(
            "T0V0/win/2026/07/23/11/20260723T103000Z_win1T0V0.jpg", ""
        )
        is None
    )
    assert image_download_timestamp(
        "pilot/T0V0/ska/2026/07/23/10/20260723T103000Z_ska1T0V0.jpg",
        "pilot",
    ) == datetime(2026, 7, 23, 10, 30, tzinfo=timezone.utc)


def test_cleanup_deletes_only_old_canonical_images():
    metrics = Metrics()
    result = cleanup_spool(
        config=config(),
        older_than_hours=24,
        dry_run=False,
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        client=Client(),
        metrics=metrics,
        include_keys=True,
    )
    assert result.examined == 3
    assert result.eligible == 1
    assert result.deleted == 1
    assert result.deleted_bytes == 100
    assert result.skipped_unknown == 1
    assert result.selected_keys == [
        "T0V0/win/2026/07/23/10/20260723T103000Z_win1T0V0.jpg"
    ]
    assert metrics.kwargs["success"] is True
