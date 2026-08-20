import os
from pathlib import Path
import uuid

import psycopg
import pytest

from config.deployment_config import DatabaseConfig
from database.healthcheck import EXPECTED_TABLES, check_database


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
        yield database_connection
        database_connection.rollback()


def test_expected_tables_and_seed_networks(connection) -> None:
    assert check_database() == EXPECTED_TABLES
    with connection.cursor() as cursor:
        cursor.execute("SELECT network_id FROM network ORDER BY network_id")
        assert [row[0] for row in cursor.fetchall()] == ["fin", "ska", "win"]


def test_json_metadata_round_trip(connection) -> None:
    suffix = uuid.uuid4().hex[:6]
    site_id = f"win{suffix}"
    stream_id = f"win{suffix}stream"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO site (
                site_id, network_id, latitude, longitude, provider_metadata
            ) VALUES (%s, 'win', 60.0, 25.0, %s)
            """,
            (site_id, psycopg.types.json.Jsonb({"title": "test camera"})),
        )
        cursor.execute(
            """
            INSERT INTO source_stream (
                source_stream_id,
                site_id,
                provider_source_stream_id,
                selected_rendition,
                provider_metadata
            ) VALUES (%s, %s, %s, 'preview', %s)
            RETURNING provider_metadata
            """,
            (
                stream_id,
                site_id,
                suffix,
                psycopg.types.json.Jsonb({"available": True}),
            ),
        )
        assert cursor.fetchone()[0] == {"available": True}


@pytest.mark.parametrize(("latitude", "longitude"), [(91.0, 0.0), (0.0, 181.0)])
def test_invalid_coordinates_are_rejected(
    connection, latitude: float, longitude: float
) -> None:
    with connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO site (site_id, network_id, latitude, longitude)
                VALUES (%s, 'win', %s, %s)
                """,
                (uuid.uuid4().hex[:16], latitude, longitude),
            )


def test_invalid_stream_status_is_rejected(connection) -> None:
    suffix = uuid.uuid4().hex[:6]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO site (site_id, network_id, latitude, longitude)
            VALUES (%s, 'win', 60.0, 25.0)
            """,
            (f"win{suffix}",),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO source_stream (
                    source_stream_id,
                    site_id,
                    provider_source_stream_id,
                    selected_rendition,
                    status
                ) VALUES (%s, %s, %s, 'preview', 'unknown')
                """,
                (f"win{suffix}stream", f"win{suffix}", suffix),
            )


def test_source_stream_requires_existing_site(connection) -> None:
    suffix = uuid.uuid4().hex[:6]
    with connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cursor.execute(
                """
                INSERT INTO source_stream (
                    source_stream_id,
                    site_id,
                    provider_source_stream_id,
                    selected_rendition
                ) VALUES (%s, %s, %s, 'preview')
                """,
                (f"win{suffix}stream", f"missing{suffix}", suffix),
            )


@pytest.mark.parametrize("site_id", ["site-with-dash", "x" * 17])
def test_site_identifier_contract_is_enforced(connection, site_id: str) -> None:
    with connection.cursor() as cursor:
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO site (site_id, network_id, latitude, longitude)
                VALUES (%s, 'win', 60.0, 25.0)
                """,
                (site_id,),
            )


@pytest.mark.parametrize("stream_id", ["stream-with-dash", "x" * 17])
def test_source_stream_identifier_contract_is_enforced(
    connection, stream_id: str
) -> None:
    site_id = f"win{uuid.uuid4().hex[:8]}"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO site (site_id, network_id, latitude, longitude)
            VALUES (%s, 'win', 60.0, 25.0)
            """,
            (site_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO source_stream (
                    source_stream_id,
                    site_id,
                    provider_source_stream_id,
                    selected_rendition
                ) VALUES (%s, %s, %s, 'preview')
                """,
                (stream_id, site_id, stream_id),
            )
