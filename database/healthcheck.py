"""Check PostgreSQL connectivity and the pilot registry schema."""

import psycopg

from config.deployment_config import DatabaseConfig


EXPECTED_TABLES = {"network", "site", "source_stream"}


def check_database(config: DatabaseConfig | None = None) -> set[str]:
    config = config or DatabaseConfig.from_environment()
    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.name,
        user=config.user,
        password=config.read_password(),
        connect_timeout=5,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (list(EXPECTED_TABLES),),
            )
            tables = {row[0] for row in cursor.fetchall()}

    missing = EXPECTED_TABLES - tables
    if missing:
        raise RuntimeError(f"Pilot database schema is incomplete: {sorted(missing)}")
    return tables


def main() -> None:
    tables = check_database()
    print(f"PostgreSQL ready; pilot tables: {', '.join(sorted(tables))}")


if __name__ == "__main__":
    main()
