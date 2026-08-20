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
                        "Key": "T0/win/2026/07/23/10/20260723T103000Z_win1T0.jpg",
                        "Size": 100,
                    },
                    {
                        "Key": "T0/fin/2026/07/24/11/20260724T110000Z_fin1T0.jpg",
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
        "T0/win/2026/07/23/10/20260723T103000Z_win1T0.jpg", ""
    ) == datetime(2026, 7, 23, 10, 30, tzinfo=timezone.utc)
    assert image_download_timestamp("backups/postgresql/database.dump", "") is None
    assert (
        image_download_timestamp(
            "T0/win/2026/07/23/11/20260723T103000Z_win1T0.jpg", ""
        )
        is None
    )
    assert image_download_timestamp(
        "pilot/T0/ska/2026/07/23/10/20260723T103000Z_ska1T0.jpg",
        "pilot",
    ) == datetime(2026, 7, 23, 10, 30, tzinfo=timezone.utc)


def test_cleanup_parser_remains_compatible_with_previous_t0v0_prefix():
    assert image_download_timestamp(
        "T0V0/win/2026/07/23/10/20260723T103000Z_win1T0V0.jpg",
        "",
        transformation_prefix="T0V0",
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
        "T0/win/2026/07/23/10/20260723T103000Z_win1T0.jpg"
    ]
    assert metrics.kwargs["success"] is True


def test_cleanup_is_scoped_to_one_transformation_by_default():
    class TwoVersionPaginator:
        def paginate(self, **kwargs):
            assert kwargs["Prefix"] == "T0/"
            return [
                {
                    "Contents": [
                        {
                            "Key": "T0/win/2026/07/23/10/20260723T103000Z_win1T0.jpg",
                            "Size": 100,
                        }
                    ]
                }
            ]

    client = Client()
    client.get_paginator = lambda _name: TwoVersionPaginator()
    result = cleanup_spool(
        config=config(),
        older_than_hours=24,
        dry_run=True,
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        client=client,
        metrics=Metrics(),
        transformation_prefix="T0",
    )
    assert result.eligible == 1


def test_scoped_cleanup_deletes_old_malformed_object_by_last_modified():
    class MalformedPaginator:
        def paginate(self, **kwargs):
            assert kwargs["Prefix"] == "T0/"
            return [
                {
                    "Contents": [
                        {
                            "Key": "T0/abandoned-object.tmp",
                            "Size": 75,
                            "LastModified": datetime(
                                2026, 7, 22, 12, tzinfo=timezone.utc
                            ),
                        },
                        {
                            "Key": "T0/recent-object.tmp",
                            "Size": 25,
                            "LastModified": datetime(
                                2026, 7, 24, 11, 30, tzinfo=timezone.utc
                            ),
                        },
                    ]
                }
            ]

    client = Client()
    client.get_paginator = lambda _name: MalformedPaginator()
    result = cleanup_spool(
        config=config(),
        older_than_hours=24,
        dry_run=False,
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        client=client,
        metrics=Metrics(),
        include_keys=True,
    )

    assert result.eligible == 1
    assert result.deleted == 1
    assert result.malformed_eligible == 1
    assert result.malformed_deleted == 1
    assert result.selected_keys == ["T0/abandoned-object.tmp"]


def test_bucket_wide_cleanup_does_not_delete_malformed_objects():
    class UnknownPaginator:
        def paginate(self, **kwargs):
            assert "Prefix" not in kwargs
            return [
                {
                    "Contents": [
                        {
                            "Key": "backups/postgresql/database.dump",
                            "Size": 999,
                            "LastModified": datetime(
                                2026, 7, 1, tzinfo=timezone.utc
                            ),
                        }
                    ]
                }
            ]

    client = Client()
    client.get_paginator = lambda _name: UnknownPaginator()
    result = cleanup_spool(
        config=config(),
        older_than_hours=24,
        dry_run=False,
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        client=client,
        metrics=Metrics(),
        all_transformation_prefixes=True,
    )

    assert result.deleted == 0
    assert result.skipped_unknown == 1
    assert result.malformed_deleted == 0
