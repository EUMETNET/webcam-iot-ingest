from datetime import date

from config.deployment_config import S3Config
from database.database_backup_cleanup import _backup_date, cleanup_database_backups


class Metrics:
    def publish(self, **kwargs):
        self.kwargs = kwargs
        return True


class Client:
    def __init__(self, keys):
        self.keys = keys
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        assert kwargs["Prefix"] == "backups/postgresql/"
        return [
            {
                "Contents": [
                    {"Key": key, "Size": index + 1}
                    for index, key in enumerate(self.keys)
                ]
            }
        ]

    def delete_objects(self, **kwargs):
        self.deleted = [row["Key"] for row in kwargs["Delete"]["Objects"]]
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


def key(day: str, timestamp: str = "120000") -> str:
    compact = day.replace("/", "")
    return (
        f"backups/postgresql/{day}/"
        f"webcam_ingestion_{compact}T{timestamp}Z.dump"
    )


def test_backup_date_rejects_malformed_or_mismatched_keys():
    assert _backup_date(key("2026/07/30"), "backups/postgresql") == date(
        2026, 7, 30
    )
    assert _backup_date(
        "backups/postgresql/2026/07/30/unexpected.dump",
        "backups/postgresql",
    ) is None


def test_cleanup_removes_latest_prior_available_day_in_same_month():
    current = key("2026/07/30")
    prior = key("2026/07/27")
    older = key("2026/07/20")
    preceding_month = key("2026/06/30")
    unexpected = "backups/postgresql/notes.txt"
    client = Client([current, prior, older, preceding_month, unexpected])

    result = cleanup_database_backups(
        config=config(),
        current_key=current,
        client=client,
        metrics=Metrics(),
        include_keys=True,
    )

    assert result.selected_keys == [prior]
    assert client.deleted == [prior]
    assert result.skipped_unknown == 1


def test_first_day_of_month_retains_preceding_month():
    current = key("2026/08/01")
    client = Client([current, key("2026/07/31")])

    result = cleanup_database_backups(
        config=config(),
        current_key=current,
        client=client,
        metrics=Metrics(),
        dry_run=True,
        include_keys=True,
    )

    assert result.selected_keys == []
