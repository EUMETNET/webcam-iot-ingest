from datetime import UTC, datetime
from unittest.mock import Mock

from database.registry_queries import PendingPublication
from ingestion.shared.publication_outbox import deliver_publication


def pending(stage="pending_s3") -> PendingPublication:
    return PendingPublication(
        "image.jpg", "win42", "marker", datetime.now(UTC), "T0V0/win/image.jpg",
        b"jpeg", {"derived_stream": {"transformation_version": "T0V0"}},
        stage, 0, None,
    )


def test_upload_is_committed_before_mqtt_and_completion(monkeypatch) -> None:
    events = []
    connection = Mock()
    storage = Mock()
    publisher = Mock()
    monkeypatch.setattr(
        "ingestion.shared.publication_outbox.mark_publication_uploaded",
        lambda *args: events.append("mark_uploaded"),
    )
    monkeypatch.setattr(
        "ingestion.shared.publication_outbox.complete_publication",
        lambda *args: events.append("complete"),
    )
    storage.upload.side_effect = lambda *args: events.append("upload")
    publisher.publish.side_effect = lambda *args: events.append("mqtt")
    connection.commit.side_effect = lambda: events.append("commit")

    result = deliver_publication(connection, storage, publisher, pending())

    assert result.outcome == "published"
    assert events == ["upload", "mark_uploaded", "commit", "mqtt", "complete", "commit"]


def test_mqtt_replay_does_not_repeat_s3(monkeypatch) -> None:
    monkeypatch.setattr(
        "ingestion.shared.publication_outbox.complete_publication", lambda *args: None
    )
    storage = Mock()
    publisher = Mock()

    deliver_publication(Mock(), storage, publisher, pending("pending_mqtt"))

    storage.upload.assert_not_called()
    publisher.publish.assert_called_once()
