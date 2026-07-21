from pathlib import Path

import pytest

from config.deployment_config import AltitudeConfig, DatabaseConfig, WindyConfig


def test_database_config_reads_password_from_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("local-test-password\n", encoding="utf-8")
    config = DatabaseConfig(
        host="localhost",
        port=5432,
        name="webcam_ingestion",
        user="webcam_ingestion",
        password_file=password_file,
        pool_size=5,
    )

    assert config.read_password() == "local-test-password"


def test_database_config_rejects_empty_password_file(tmp_path: Path) -> None:
    password_file = tmp_path / "database_password"
    password_file.write_text("\n", encoding="utf-8")
    config = DatabaseConfig(
        host="localhost",
        port=5432,
        name="webcam_ingestion",
        user="webcam_ingestion",
        password_file=password_file,
        pool_size=5,
    )

    with pytest.raises(ValueError, match="password file is empty"):
        config.read_password()


def test_altitude_config_defaults_to_open_meteo() -> None:
    config = AltitudeConfig.from_environment()

    assert config.enabled is True
    assert config.provider_url == "https://api.open-meteo.com/v1/elevation"
    assert config.batch_size == 100
    assert config.max_sites_per_run == 5000


def test_windy_config_loads_query_discs(monkeypatch, tmp_path: Path) -> None:
    areas_file = tmp_path / "areas.json"
    areas_file.write_text(
        '[{"latitude":60,"longitude":25,"radius_km":100,"countries":["fi","SE"]}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("WINDY_DISCOVERY_AREAS_FILE", str(areas_file))
    config = WindyConfig.from_environment()

    assert config.discovery_areas[0].countries == ("FI", "SE")
    assert config.discovery_areas[0].radius_km == 100
    assert config.site_distance_threshold_m == 10
    assert len(config.member_countries) == 33


def test_windy_config_requires_query_discs(monkeypatch, tmp_path: Path) -> None:
    areas_file = tmp_path / "areas.json"
    areas_file.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("WINDY_DISCOVERY_AREAS_FILE", str(areas_file))
    with pytest.raises(ValueError, match="at least one Windy discovery area"):
        WindyConfig.from_environment()


def test_windy_config_requires_whole_kilometre_radius(
    monkeypatch, tmp_path: Path
) -> None:
    areas_file = tmp_path / "areas.json"
    areas_file.write_text(
        '[{"latitude":60,"longitude":25,"radius_km":50.5,"countries":["FI"]}]',
        encoding="utf-8",
    )
    monkeypatch.setenv("WINDY_DISCOVERY_AREAS_FILE", str(areas_file))

    with pytest.raises(ValueError, match="radius must use whole kilometres"):
        WindyConfig.from_environment()


def test_windy_config_rejects_invalid_query_disc_file(
    monkeypatch, tmp_path: Path
) -> None:
    areas_file = tmp_path / "areas.json"
    areas_file.write_text("not JSON", encoding="utf-8")
    monkeypatch.setenv("WINDY_DISCOVERY_AREAS_FILE", str(areas_file))

    with pytest.raises(ValueError, match="discovery areas file is invalid"):
        WindyConfig.from_environment()


def test_windy_config_rejects_missing_query_disc_file(
    monkeypatch, tmp_path: Path
) -> None:
    areas_file = tmp_path / "missing.json"
    monkeypatch.setenv("WINDY_DISCOVERY_AREAS_FILE", str(areas_file))

    with pytest.raises(ValueError, match="cannot read Windy discovery areas file"):
        WindyConfig.from_environment()


def test_windy_config_reads_api_key_from_file(tmp_path: Path) -> None:
    key_file = tmp_path / "windy_api_key"
    key_file.write_text("windy-test-key\n", encoding="utf-8")
    config = WindyConfig(
        api_key_file=key_file,
        discovery_areas=(),
        site_distance_threshold_m=100,
        request_timeout_s=15,
    )
    assert config.read_api_key() == "windy-test-key"
