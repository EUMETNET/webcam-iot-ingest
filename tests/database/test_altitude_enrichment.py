import os
import uuid

import psycopg
import pytest

from config.deployment_config import DatabaseConfig
from database.registry_queries import DiscoveredSite, apply_discovery_update
from discovery.shared.add_altitude import enrich_missing_altitudes


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DATABASE_TESTS") != "1",
    reason="set RUN_DATABASE_TESTS=1 to run PostgreSQL integration tests",
)


class FakeAltitudeClient:
    def __init__(self, values: tuple[float | None, ...]) -> None:
        self.values = values
        self.calls: list[list[tuple[float, float]]] = []

    def lookup(self, coordinates):
        self.calls.append(list(coordinates))
        return self.values[: len(coordinates)]


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


def _site(site_id: str, altitude: float | None = None) -> DiscoveredSite:
    return DiscoveredSite(
        site_id=site_id,
        provider_site_id=site_id,
        latitude=60.0,
        longitude=25.0,
        altitude=altitude,
        country="FI",
    )


def test_dry_run_write_and_repeat_are_safe(connection) -> None:
    suffix = uuid.uuid4().hex[:6]
    network = f"alt{suffix}"
    missing_id = f"{network}a"
    existing_id = f"{network}b"
    connection.execute(
        "INSERT INTO network (network_id, network_name) VALUES (%s, %s)",
        (network, "Altitude test"),
    )
    apply_discovery_update(
        connection,
        network,
        [_site(missing_id), _site(existing_id, 99.0)],
        [],
    )

    dry_client = FakeAltitudeClient((42.0,))
    dry = enrich_missing_altitudes(
        connection, network, dry_client, dry_run=True, batch_size=100
    )
    assert (dry.eligible, dry.resolved, dry.updated) == (1, 1, 0)
    assert connection.execute(
        "SELECT altitude FROM site WHERE site_id = %s", (missing_id,)
    ).fetchone()[0] is None

    applied = enrich_missing_altitudes(
        connection, network, FakeAltitudeClient((42.0,)), batch_size=100
    )
    assert (applied.eligible, applied.resolved, applied.updated) == (1, 1, 1)
    assert connection.execute(
        "SELECT altitude FROM site WHERE site_id = %s", (missing_id,)
    ).fetchone()[0] == 42.0
    assert connection.execute(
        "SELECT altitude FROM site WHERE site_id = %s", (existing_id,)
    ).fetchone()[0] == 99.0

    repeated = enrich_missing_altitudes(
        connection, network, FakeAltitudeClient(()), batch_size=100
    )
    assert repeated.eligible == repeated.updated == 0


def test_coordinate_guard_prevents_stale_altitude_write(connection) -> None:
    suffix = uuid.uuid4().hex[:6]
    network = f"alt{suffix}"
    site_id = f"{network}site"
    connection.execute(
        "INSERT INTO network (network_id, network_name) VALUES (%s, %s)",
        (network, "Altitude test"),
    )
    apply_discovery_update(connection, network, [_site(site_id)], [])

    class MovingClient(FakeAltitudeClient):
        def lookup(self, coordinates):
            connection.execute(
                "UPDATE site SET latitude = 61.0 WHERE site_id = %s", (site_id,)
            )
            return (42.0,)

    result = enrich_missing_altitudes(
        connection, network, MovingClient((42.0,)), batch_size=100
    )
    assert result.resolved == 1
    assert result.updated == 0
    assert connection.execute(
        "SELECT altitude FROM site WHERE site_id = %s", (site_id,)
    ).fetchone()[0] is None
