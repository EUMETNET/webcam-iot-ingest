from pathlib import Path

import pytest

from config.deployment_config import (
    AltitudeConfig,
    DatabaseConfig,
    DiscoveryMetricsConfig,
    FintrafficConfig,
    FintrafficIngestionConfig,
    SkapingConfig,
    SkapingIngestionConfig,
    WindyConfig,
    WindyIngestionConfig,
    TransformationConfig,
    MqttConfig,
    WorkerConfig,
)


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


def test_discovery_metrics_config_has_batch_gateway_defaults(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DISCOVERY_METRICS_ENABLED", raising=False)
    monkeypatch.delenv("DISCOVERY_METRICS_GATEWAY_URL", raising=False)
    config = DiscoveryMetricsConfig.from_environment()

    assert config.enabled is True
    assert config.gateway_url == "http://localhost:9091"
    assert config.push_timeout_s == 5


def test_discovery_metrics_config_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DISCOVERY_METRICS_ENABLED", "false")
    assert DiscoveryMetricsConfig.from_environment().enabled is False


def test_period_direct_replacement_modulus_defaults_per_network(
    monkeypatch,
) -> None:
    for name in (
        "WINDY_PERIOD_DIRECT_REPLACEMENT_MODULUS",
        "FINTRAFFIC_PERIOD_DIRECT_REPLACEMENT_MODULUS",
        "SKAPING_PERIOD_DIRECT_REPLACEMENT_MODULUS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert (
        WindyIngestionConfig.from_environment().period_direct_replacement_modulus
        == 250
    )
    assert (
        FintrafficIngestionConfig.from_environment().period_direct_replacement_modulus
        == 250
    )
    assert (
        SkapingIngestionConfig.from_environment().period_direct_replacement_modulus
        == 250
    )


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


def test_fintraffic_config_has_safe_discovery_defaults(monkeypatch) -> None:
    monkeypatch.delenv("FINTRAFFIC_USER_HEADER", raising=False)
    config = FintrafficConfig.from_environment()

    assert config.user_header == "webcam-iot-ingest"
    assert config.stations_url.endswith("/api/weathercam/v1/stations")
    assert config.selected_collection_status == "GATHERING"
    assert config.require_in_collection is True
    assert config.selected_rendition == "full_jpeg"


def test_fintraffic_config_rejects_header_injection(monkeypatch) -> None:
    monkeypatch.setenv("FINTRAFFIC_USER_HEADER", "application\nInjected: value")

    with pytest.raises(ValueError, match="line breaks"):
        FintrafficConfig.from_environment()


def test_skaping_config_has_safe_discovery_defaults(monkeypatch) -> None:
    monkeypatch.delenv("SKAPING_DISCOVERY_MIN_CAMERAS", raising=False)
    config = SkapingConfig.from_environment()

    assert config.summary_url == "https://api.skaping.com/camera/summaryApp"
    assert config.minimum_camera_count == 20
    assert config.selected_rendition == "mini"
    assert len(config.member_countries) == 33


def test_skaping_config_reads_api_key_from_file(tmp_path: Path) -> None:
    key_file = tmp_path / "skaping_api_key"
    key_file.write_text("skaping-test-key\n", encoding="utf-8")
    config = SkapingConfig(
        api_key_file=key_file,
        summary_url="https://api.skaping.com/camera/summaryApp",
        request_timeout_s=15,
        retry_count=2,
        retry_backoff_s=1,
        request_delay_s=0,
        minimum_camera_count=1,
    )

    assert config.read_api_key() == "skaping-test-key"


def test_skaping_config_rejects_unsafe_empty_snapshot_threshold(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SKAPING_DISCOVERY_MIN_CAMERAS", "0")

    with pytest.raises(ValueError, match="minimum camera count"):
        SkapingConfig.from_environment()


def test_windy_ingestion_config_has_bounded_safe_defaults() -> None:
    config = WindyIngestionConfig.from_environment()

    assert config.default_limit == 10
    assert config.freshness_query_retry_count == 0
    assert config.download_retry_count == 0
    assert config.image_max_bytes == 10_000_000
    assert config.minimum_ingestion_interval_s == 300
    assert config.minimum_polling_interval_s == 540
    assert config.polling_interval_factor == 0.7
    assert WorkerConfig.from_environment().initial_stagger_window_s == 600
    assert WorkerConfig.from_environment().readiness_window_s == 600


def test_skaping_ingestion_config_has_zero_retry_defaults() -> None:
    config = SkapingIngestionConfig.from_environment()

    assert config.default_limit == 10
    assert config.freshness_query_retry_count == 0
    assert config.download_retry_count == 0
    assert config.minimum_ingestion_interval_s == 300
    assert config.minimum_polling_interval_s == 240
    assert config.polling_interval_factor == 0.7


def test_fintraffic_ingestion_config_has_eight_minute_polling_floor() -> None:
    config = FintrafficIngestionConfig.from_environment()

    assert config.minimum_polling_interval_s == 480


def test_checkpoint6_configuration_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MQTT_HOST", raising=False)
    transformation = TransformationConfig.from_environment()
    mqtt = MqttConfig.from_environment()

    assert transformation.version == "T0V0"
    assert transformation.max_height_px == 288
    assert transformation.target_size_bytes == 50_000
    assert transformation.panoramic_target_size_bytes == 200_000
    assert mqtt.host == "localhost"
    assert mqtt.qos == 1
