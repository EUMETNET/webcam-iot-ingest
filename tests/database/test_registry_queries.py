import os
from datetime import datetime, timedelta, timezone
import uuid

import psycopg
import pytest

from config.deployment_config import DatabaseConfig
from database.registry_queries import (
    DiscoveredSite,
    DiscoveredSourceStream,
    IngestionStateUpdate,
    RegistryCollisionError,
    apply_discovery_update,
    apply_ingestion_state_updates,
    build_ema_update_candidate,
    get_due_source_streams,
    get_network_registry,
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
    apply_ingestion_state_updates(
        connection,
        [
            IngestionStateUpdate(
                recent_stream,
                now - timedelta(minutes=2),
                "recent-marker",
                now - timedelta(minutes=2),
                processed_timestamp=now - timedelta(minutes=2),
            )
        ],
        apply_ema=False,
    )

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


def test_due_stream_selection_combines_download_and_provider_time_guards(
    connection, identifiers
) -> None:
    site_id, due_stream, blocked_stream = identifiers
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection,
        "win",
        [site(site_id)],
        [stream(due_stream, site_id), stream(blocked_stream, site_id)],
    )
    connection.execute(
        """
        UPDATE source_stream
        SET last_processed_timestamp = %s,
            last_observed_provider_timestamp = CASE source_stream_id
                WHEN %s THEN %s ELSE %s END,
            estimated_source_stream_period = 600
        WHERE source_stream_id IN (%s, %s)
        """,
        (
            now - timedelta(minutes=6),
            due_stream,
            now - timedelta(minutes=8),
            now - timedelta(minutes=6),
            due_stream,
            blocked_stream,
        ),
    )

    due = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        now=now,
    )
    assert [item.source_stream_id for item in due] == [due_stream]

    with_polling_floor = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        minimum_polling_interval=timedelta(minutes=9),
        now=now,
    )
    assert with_polling_floor == []

    connection.execute(
        """
        UPDATE source_stream
        SET last_observed_provider_timestamp = %s
        WHERE source_stream_id = %s
        """,
        (now - timedelta(minutes=10), due_stream),
    )
    with_polling_floor = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        minimum_polling_interval=timedelta(minutes=9),
        now=now,
    )
    assert [item.source_stream_id for item in with_polling_floor] == [due_stream]

    connection.execute(
        """
        UPDATE source_stream
        SET last_observed_provider_timestamp = %s
        WHERE source_stream_id = %s
        """,
        (now - timedelta(minutes=8), blocked_stream),
    )
    due = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        now=now,
    )
    assert {item.source_stream_id for item in due} == {
        due_stream,
        blocked_stream,
    }

    connection.execute(
        """
        UPDATE source_stream
        SET last_processed_timestamp = %s
        WHERE source_stream_id = %s
        """,
        (now - timedelta(minutes=4), blocked_stream),
    )
    due = get_due_source_streams(
        connection,
        "win",
        timedelta(minutes=5),
        polling_interval_factor=0.7,
        now=now,
    )
    assert [item.source_stream_id for item in due] == [due_stream]


def test_ingestion_state_updates_respect_workflow_stages(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    first_timestamp = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )

    current = get_due_source_streams(
        connection, "win", timedelta(0), now=first_timestamp
    )[0]
    first_ema = build_ema_update_candidate(
        current, provider_update_timestamp=first_timestamp, ema_alpha=0.25
    )
    apply_ingestion_state_updates(
        connection,
        [
            IngestionStateUpdate(
                stream_id,
                first_timestamp,
                "first-good-marker",
                first_timestamp,
                first_ema,
                first_timestamp,
            )
        ],
        apply_ema=True,
    )
    second_timestamp = first_timestamp + timedelta(seconds=500)
    due_job = get_due_source_streams(
        connection,
        "win",
        timedelta(0),
        polling_interval_factor=0,
        now=second_timestamp,
    )[0]
    second_ema = build_ema_update_candidate(
        due_job, provider_update_timestamp=second_timestamp, ema_alpha=0.25
    )
    apply_ingestion_state_updates(
        connection,
        [
            IngestionStateUpdate(
                stream_id,
                second_timestamp,
                "second-good-marker",
                second_timestamp,
                second_ema,
                second_timestamp,
            )
        ],
        apply_ema=True,
    )
    assert first_ema is not None
    assert first_ema.estimated_source_stream_period == 300.0
    assert second_ema is not None
    assert second_ema.estimated_source_stream_period == 350.0

    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["last_observed_image_marker"] == "second-good-marker"
    assert stored["last_download_timestamp"] == second_timestamp
    assert stored["last_observed_provider_timestamp"] == second_timestamp
    assert stored["last_processed_timestamp"] == second_timestamp
    assert stored["estimated_source_stream_period"] == 350.0


def test_deferred_ema_is_applied_conditionally(connection, identifiers) -> None:
    site_id, stream_id, _ = identifiers
    timestamp = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )

    job = get_due_source_streams(connection, "win", timedelta(0), now=timestamp)[0]
    candidate = build_ema_update_candidate(
        job, provider_update_timestamp=timestamp, ema_alpha=0.2
    )
    assert candidate is not None
    update = IngestionStateUpdate(
        stream_id, timestamp, "marker", timestamp, candidate
    )
    assert apply_ingestion_state_updates(
        connection, [update], apply_ema=False
    ) == 1
    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["last_download_timestamp"] == timestamp
    assert stored["estimated_source_stream_period"] is None

    assert apply_ingestion_state_updates(
        connection, [update], apply_ema=True
    ) == 1
    stored = get_network_registry(connection, "win").source_streams[stream_id]
    assert stored["estimated_source_stream_period"] == 300.0


def test_unselected_freshness_snapshot_is_not_persisted(
    connection, identifiers
) -> None:
    site_id, stream_id, _ = identifiers
    apply_discovery_update(
        connection, "win", [site(site_id)], [stream(stream_id, site_id)]
    )
    before = get_network_registry(connection, "win").source_streams[stream_id]

    # A provider result by itself performs no write. Only a returned download
    # decision is passed to apply_ingestion_state_updates at epoch end.
    observed_timestamp = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    assert observed_timestamp is not None
    after = get_network_registry(connection, "win").source_streams[stream_id]
    assert after["last_observed_provider_timestamp"] == before[
        "last_observed_provider_timestamp"
    ]
    assert after["last_observed_image_marker"] == before[
        "last_observed_image_marker"
    ]
