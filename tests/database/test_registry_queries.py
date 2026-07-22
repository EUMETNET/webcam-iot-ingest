import os
from datetime import datetime, timedelta, timezone
import uuid

import psycopg
import pytest

from config.deployment_config import DatabaseConfig
from database.registry_queries import (
    DiscoveredSite,
    DiscoveredSourceStream,
    RegistryCollisionError,
    apply_discovery_update,
    apply_ema_update_candidates,
    get_due_source_streams,
    get_pending_publications,
    get_network_registry,
    record_freshness_query,
    record_provider_image_marker,
    record_download_and_enqueue_publication,
    mark_publication_uploaded,
    record_publication_failure,
    complete_publication,
    record_successful_download,
    set_source_stream_status,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="set RUN_DATABASE_TESTS=1 to run PostgreSQL integration tests",
)


@pytest.fixture()
def connection():
    config = DatabaseConfig.from_environment()
    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.name,
        user=config.user,
        password=config.read_password(),
    ) as database_connection:
        database_connection.execute("BEGIN")
        yield database_connection
        database_connection.rollback()


@pytest.fixture()
def identifiers() -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex
    return f"win{suffix}", f"win{suffix}a", f"win{suffix}b"


def site(site_id: str, *, title: str = "first") -> DiscoveredSite:
    return DiscoveredSite(
        site_id=site_id,
        provider_site_id=site_id.removeprefix("win"),
        latitude=60.0,
        longitude=25.0,
        country="FI",
        provider_metadata={"title": title},
    )


def stream(
    stream_id: str, site_id: str, *, available: bool = True
) -> DiscoveredSourceStream:
    return DiscoveredSourceStream(
        source_stream_id=stream_id,
        site_id=site_id,
        provider_source_stream_id=stream_id.removeprefix("win"),
        selected_rendition="preview",
        provider_metadata={"available": available},
    )


def test_discovery_insert_is_idempotent_and_replaces_metadata(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    first = apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )
    second = apply_discovery_update(
        connection,
        "win",
        [site(site_id, title="replacement")],
        [stream(stream_id, site_id, available=False)],
    )

    assert first.sites_inserted == first.streams_inserted == 1
    assert second.sites_updated == second.streams_updated == 1
    snapshot = get_network_registry(connection, "win")
    assert snapshot.sites[site_id]["provider_metadata"] == {"title": "replacement"}
    assert snapshot.source_streams[stream_id]["provider_metadata"] == {
        "available": False
    }
    assert snapshot.source_streams[stream_id]["status"] == "active"


def test_discovery_inactivates_missing_and_reactivates_returning_stream(
    connection, identifiers
) -> None:
    site_id, stream_a, stream_b = identifiers
    apply_discovery_update(
        connection,
        "win",
        [site(site_id)],
        [stream(stream_a, site_id), stream(stream_b, site_id)],
    )

    removed = apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_a, site_id)]
    )
    assert removed.streams_inactivated == 1
    assert get_network_registry(connection, "win").source_streams[stream_b][
        "status"
    ] == "inactive"

    returned = apply_discovery_update(
        connection,
        "win",
        [site(site_id)],
        [stream(stream_a, site_id), stream(stream_b, site_id)],
    )
    assert returned.streams_activated == 1
    assert get_network_registry(connection, "win").source_streams[stream_b][
        "status"
    ] == "active"




def test_discovery_never_overrides_blacklist(connection, identifiers) -> None:
    site_id, stream_id, _ = identifiers
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )
    set_source_stream_status(connection, stream_id, "blacklisted")

    present = apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )
    assert present.blacklisted_preserved == 1
    assert get_network_registry(connection, "win").source_streams[stream_id][
        "status"
    ] == "blacklisted"

    absent = apply_discovery_update(connection, "win", [site(site_id)], [])
    assert absent.blacklisted_preserved == 1
    assert get_network_registry(connection, "win").source_streams[stream_id][
        "status"
    ] == "blacklisted"


def test_identifier_collision_preserves_original_assignment(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    original_site = site(site_id)
    apply_discovery_update(
        connection, "win", [original_site], [stream(stream_id, site_id)]
    )
    colliding = DiscoveredSite(
        site_id=site_id,
        provider_site_id="another-provider-id",
        latitude=61.0,
        longitude=26.0,
    )

    with pytest.raises(RegistryCollisionError):
        apply_discovery_update(connection, "win", [colliding], [])

    stored = get_network_registry(connection, "win").sites[site_id]
    assert stored["provider_site_id"] == original_site.provider_site_id
    assert stored["latitude"] == original_site.latitude


def test_discovery_update_rolls_back_as_one_transaction(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    invalid_stream = DiscoveredSourceStream(
        source_stream_id=stream_id,
        site_id=site_id,
        provider_source_stream_id="provider-stream",
        selected_rendition="",
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        apply_discovery_update(
            connection, "win", [site(site_id)], [invalid_stream]
        )

    assert site_id not in get_network_registry(connection, "win").sites


def test_due_stream_selection_filters_status_network_and_recent_downloads(
    connection, identifiers
) -> None:
    site_id, due_stream, recent_stream = identifiers
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection,
        "win",
        [site(site_id)],
        [stream(due_stream, site_id), stream(recent_stream, site_id)],
    )
    record_successful_download(
        connection,
        recent_stream,
        "recent-marker",
        now - timedelta(minutes=2),
        ema_alpha=0.5,
    )
    record_freshness_query(connection, recent_stream, now - timedelta(minutes=2))

    due = get_due_source_streams(
        connection, "win", timedelta(minutes=5), now=now
    )
    assert [item.source_stream_id for item in due] == [due_stream]

    set_source_stream_status(connection, due_stream, "inactive")
    assert (
        get_due_source_streams(
            connection, "win", timedelta(minutes=5), now=now
        )
        == []
    )


def test_due_stream_selection_filters_country_and_applies_limit(
    connection, identifiers
) -> None:
    site_id, stream_a, stream_b = identifiers
    second_site_id = f"{site_id}c"
    apply_discovery_update(
        connection,
        "win",
        [site(site_id), site(second_site_id)],
        [stream(stream_a, site_id), stream(stream_b, second_site_id)],
    )
    connection.execute(
        "UPDATE site SET country = 'DK' WHERE site_id = %s", (site_id,)
    )
    connection.execute(
        "UPDATE site SET country = 'MT' WHERE site_id = %s", (second_site_id,)
    )

    selected = get_due_source_streams(
        connection, "win", timedelta(0), countries=("DK",), limit=1
    )

    assert [item.source_stream_id for item in selected] == [stream_a]


def test_due_stream_selection_uses_ema_factor_and_maximum_cap(
    connection, identifiers
) -> None:
    site_id, short_ema_stream, capped_stream = identifiers
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection,
        "win",
        [site(site_id)],
        [stream(short_ema_stream, site_id), stream(capped_stream, site_id)],
    )
    connection.execute(
        """
        UPDATE source_stream
        SET last_freshness_query_timestamp = %s,
            ema_download_period = CASE source_stream_id WHEN %s THEN 600 ELSE 10000 END
        WHERE source_stream_id IN (%s, %s)
        """,
        (
            now - timedelta(minutes=10),
            short_ema_stream,
            short_ema_stream,
            capped_stream,
        ),
    )

    due = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        maximum_poll_interval=timedelta(minutes=30),
        now=now,
    )
    assert [item.source_stream_id for item in due] == [short_ema_stream]

    record_freshness_query(
        connection, capped_stream, now - timedelta(minutes=31)
    )
    due = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        maximum_poll_interval=timedelta(minutes=30),
        now=now,
    )
    assert {item.source_stream_id for item in due} == {
        short_ema_stream,
        capped_stream,
    }


def test_ingestion_state_updates_respect_workflow_stages(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    first_timestamp = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )

    record_provider_image_marker(connection, stream_id, "invalid-image-marker")
    marker_only = get_network_registry(connection, "win").source_streams[stream_id]
    assert marker_only["last_provider_image_marker"] == "invalid-image-marker"
    assert marker_only["last_download_timestamp"] is None
    assert marker_only["ema_download_period"] is None

    first_ema = record_successful_download(
        connection,
        stream_id,
        "first-good-marker",
        first_timestamp,
        ema_alpha=0.25,
    )
    second_ema = record_successful_download(
        connection,
        stream_id,
        "second-good-marker",
        first_timestamp + timedelta(seconds=500),
        ema_alpha=0.25,
    )
    assert first_ema == 300.0
    assert second_ema == 350.0

    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["last_provider_image_marker"] == "second-good-marker"
    assert stored["last_download_timestamp"] == first_timestamp + timedelta(
        seconds=500
    )
    assert stored["ema_download_period"] == 350.0


def test_publication_outbox_lifecycle_is_durable(connection, identifiers) -> None:
    site_id, stream_id, _ = identifiers
    timestamp = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )

    ema = record_download_and_enqueue_publication(
        connection,
        stream_id,
        "marker",
        timestamp,
        ema_alpha=0.2,
        image_id=f"image{stream_id}.jpg",
        object_key=f"T0V0/win/{stream_id}.jpg",
        derived_content=b"jpeg",
        notification={"derived_stream": {"transformation_version": "T0V0"}},
    )
    pending = get_pending_publications(connection, limit=10)

    assert ema == 300.0
    assert len(pending) == 1
    assert pending[0].stage == "pending_s3"
    assert pending[0].derived_content == b"jpeg"

    mark_publication_uploaded(connection, pending[0].image_id)
    record_publication_failure(connection, pending[0].image_id, "mqtt_publish")
    replay = get_pending_publications(connection, limit=10)[0]
    assert replay.stage == "pending_mqtt"
    assert replay.attempt_count == 1
    assert replay.last_error_code == "mqtt_publish"

    complete_publication(connection, replay.image_id)
    assert get_pending_publications(connection, limit=10) == []


def test_deferred_ema_is_applied_conditionally(connection, identifiers) -> None:
    site_id, stream_id, _ = identifiers
    timestamp = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )

    candidate = record_successful_download(
        connection,
        stream_id,
        "marker",
        timestamp,
        ema_alpha=0.2,
        defer_ema_update=True,
    )
    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["last_download_timestamp"] == timestamp
    assert stored["ema_download_period"] is None

    assert apply_ema_update_candidates(connection, [candidate]) == 1
    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["ema_download_period"] == 300.0

    record_successful_download(
        connection,
        stream_id,
        "newer-marker",
        timestamp + timedelta(minutes=5),
        ema_alpha=0.2,
        defer_ema_update=True,
    )
    assert apply_ema_update_candidates(connection, [candidate]) == 0
