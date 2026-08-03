"""Deployment configuration loaded without embedding secret values."""

from dataclasses import dataclass
import json
import os
from pathlib import Path


EUMETNET_MEMBER_COUNTRIES = (
    "AT", "BE", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IS", "IE", "IT", "LV", "LU", "ME", "NL", "LT",
    "MT", "NO", "PL", "PT", "RO", "RS", "SK", "SI", "ES", "SE",
    "CH", "MK", "GB",
)


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password_file: Path
    pool_size: int

    @classmethod
    def from_environment(cls) -> "DatabaseConfig":
        return cls(
            host=os.getenv("DATABASE_HOST", "localhost"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            name=os.getenv("DATABASE_NAME", "webcam_ingestion"),
            user=os.getenv("DATABASE_USER", "webcam_ingestion"),
            password_file=Path(
                os.getenv("DATABASE_PASSWORD_FILE", ".secrets/database_password")
            ),
            pool_size=int(os.getenv("DATABASE_POOL_SIZE", "5")),
        )

    def read_password(self) -> str:
        password = self.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError(f"Database password file is empty: {self.password_file}")
        return password


@dataclass(frozen=True)
class DiscoveryMetricsConfig:
    enabled: bool
    gateway_url: str
    push_timeout_s: float

    @classmethod
    def from_environment(cls) -> "DiscoveryMetricsConfig":
        enabled_value = os.getenv("DISCOVERY_METRICS_ENABLED", "true").lower()
        if enabled_value not in {"true", "false"}:
            raise ValueError("DISCOVERY_METRICS_ENABLED must be true or false")
        config = cls(
            enabled=enabled_value == "true",
            gateway_url=os.getenv(
                "DISCOVERY_METRICS_GATEWAY_URL", "http://localhost:9091"
            ).rstrip("/"),
            push_timeout_s=float(
                os.getenv("DISCOVERY_METRICS_PUSH_TIMEOUT_S", "5")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.gateway_url.startswith(("http://", "https://")):
            raise ValueError(
                "discovery metrics gateway URL must use HTTP or HTTPS"
            )
        if self.push_timeout_s <= 0:
            raise ValueError(
                "discovery metrics push timeout must be positive"
            )


@dataclass(frozen=True)
class BatchMetricsConfig:
    enabled: bool
    gateway_url: str
    push_timeout_s: float

    @classmethod
    def from_environment(cls) -> "BatchMetricsConfig":
        enabled_value = os.getenv("BATCH_METRICS_ENABLED", "true").lower()
        if enabled_value not in {"true", "false"}:
            raise ValueError("BATCH_METRICS_ENABLED must be true or false")
        config = cls(
            enabled=enabled_value == "true",
            gateway_url=os.getenv(
                "BATCH_METRICS_GATEWAY_URL",
                os.getenv("DISCOVERY_METRICS_GATEWAY_URL", "http://localhost:9091"),
            ).rstrip("/"),
            push_timeout_s=float(os.getenv("BATCH_METRICS_PUSH_TIMEOUT_S", "5")),
        )
        if not config.gateway_url.startswith(("http://", "https://")):
            raise ValueError("batch metrics gateway URL must use HTTP or HTTPS")
        if config.push_timeout_s <= 0:
            raise ValueError("batch metrics push timeout must be positive")
        return config


@dataclass(frozen=True)
class AltitudeConfig:
    enabled: bool
    provider_url: str
    request_timeout_s: float
    batch_size: int = 100
    request_delay_s: float = 0.1
    max_attempts: int = 3
    max_sites_per_run: int = 5000

    @classmethod
    def from_environment(cls) -> "AltitudeConfig":
        enabled_value = os.getenv("ALTITUDE_PROVIDER_ENABLED", "true").lower()
        if enabled_value not in {"true", "false"}:
            raise ValueError("ALTITUDE_PROVIDER_ENABLED must be true or false")
        config = cls(
            enabled=enabled_value == "true",
            provider_url=os.getenv(
                "ALTITUDE_PROVIDER_URL", "https://api.open-meteo.com/v1/elevation"
            ),
            request_timeout_s=float(
                os.getenv("ALTITUDE_REQUEST_TIMEOUT_S", "15")
            ),
            batch_size=int(os.getenv("ALTITUDE_BATCH_SIZE", "100")),
            request_delay_s=float(os.getenv("ALTITUDE_REQUEST_DELAY_S", "0.1")),
            max_attempts=int(os.getenv("ALTITUDE_MAX_ATTEMPTS", "3")),
            max_sites_per_run=int(
                os.getenv("ALTITUDE_MAX_SITES_PER_RUN", "5000")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.provider_url.startswith(("http://", "https://")):
            raise ValueError("altitude provider URL must use HTTP or HTTPS")
        if self.request_timeout_s <= 0:
            raise ValueError("altitude request timeout must be positive")
        if not 1 <= self.batch_size <= 100:
            raise ValueError("altitude batch size must be between 1 and 100")
        if self.request_delay_s < 0:
            raise ValueError("altitude request delay cannot be negative")
        if self.max_attempts < 1:
            raise ValueError("altitude maximum attempts must be positive")
        if self.max_sites_per_run < 1:
            raise ValueError("altitude sites per run must be positive")


@dataclass(frozen=True)
class WindyDiscoveryArea:
    latitude: float
    longitude: float
    radius_km: float
    countries: tuple[str, ...]


@dataclass(frozen=True)
class WindyConfig:
    api_key_file: Path
    discovery_areas: tuple[WindyDiscoveryArea, ...]
    site_distance_threshold_m: float
    request_timeout_s: float
    member_countries: tuple[str, ...] = EUMETNET_MEMBER_COUNTRIES
    request_delay_s: float = 0.1
    discovery_cache_file: Path | None = None
    page_size: int = 50
    selected_rendition: str = "preview"

    @classmethod
    def from_environment(cls) -> "WindyConfig":
        areas_file = Path(
            os.getenv(
                "WINDY_DISCOVERY_AREAS_FILE",
                "discovery/windy/discovery_areas.json",
            )
        )
        try:
            parsed_areas = json.loads(areas_file.read_text(encoding="utf-8"))
            areas = tuple(
                WindyDiscoveryArea(
                    latitude=float(area["latitude"]),
                    longitude=float(area["longitude"]),
                    radius_km=float(area["radius_km"]),
                    countries=tuple(
                        str(code).upper() for code in area["countries"]
                    ),
                )
                for area in parsed_areas
            )
        except OSError as error:
            raise ValueError(
                f"cannot read Windy discovery areas file: {areas_file}"
            ) from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                f"Windy discovery areas file is invalid: {areas_file}"
            ) from error

        config = cls(
            api_key_file=Path(
                os.getenv("WINDY_API_KEY_FILE", ".secrets/windy_api_key")
            ),
            discovery_areas=areas,
            site_distance_threshold_m=float(
                os.getenv("WINDY_SITE_DISTANCE_THRESHOLD_M", "10")
            ),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            member_countries=tuple(
                code.strip().upper()
                for code in os.getenv(
                    "WINDY_MEMBER_COUNTRIES", ",".join(EUMETNET_MEMBER_COUNTRIES)
                ).split(",")
                if code.strip()
            ),
            request_delay_s=float(os.getenv("WINDY_REQUEST_DELAY_S", "1")),
            discovery_cache_file=(
                Path(value)
                if (value := os.getenv("WINDY_DISCOVERY_CACHE_FILE", ".discovery-cache/windy_pages.json"))
                else None
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.discovery_areas:
            raise ValueError("at least one Windy discovery area must be configured")
        if self.site_distance_threshold_m < 0:
            raise ValueError("Windy site distance threshold cannot be negative")
        if self.request_timeout_s <= 0:
            raise ValueError("provider request timeout must be positive")
        if self.request_delay_s < 0:
            raise ValueError("Windy request delay cannot be negative")
        if not self.member_countries or any(
            len(code) != 2 or not code.isalpha() for code in self.member_countries
        ):
            raise ValueError("Windy member countries must be ISO alpha-2 codes")
        if not 1 <= self.page_size <= 50:
            raise ValueError("Windy page size must be between 1 and 50")
        for area in self.discovery_areas:
            if not -90 <= area.latitude <= 90:
                raise ValueError("Windy discovery latitude is invalid")
            if not -180 <= area.longitude <= 180:
                raise ValueError("Windy discovery longitude is invalid")
            if not 0 < area.radius_km <= 250:
                raise ValueError("Windy discovery radius must be between 0 and 250 km")
            if not area.radius_km.is_integer():
                raise ValueError("Windy discovery radius must use whole kilometres")
            if not area.countries:
                raise ValueError("each Windy discovery area needs country filters")
            if any(len(code) != 2 or not code.isalpha() for code in area.countries):
                raise ValueError("Windy country filters must be ISO alpha-2 codes")

    def read_api_key(self) -> str:
        api_key = self.api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError(f"Windy API key file is empty: {self.api_key_file}")
        return api_key


@dataclass(frozen=True)
class FintrafficConfig:
    user_header: str
    stations_url: str
    request_timeout_s: float
    retry_count: int
    retry_backoff_s: float
    request_delay_s: float
    selected_collection_status: str
    require_in_collection: bool
    selected_rendition: str = "full_jpeg"

    @classmethod
    def from_environment(cls) -> "FintrafficConfig":
        require_in_collection = os.getenv(
            "FINTRAFFIC_REQUIRE_IN_COLLECTION", "true"
        ).lower()
        if require_in_collection not in {"true", "false"}:
            raise ValueError(
                "FINTRAFFIC_REQUIRE_IN_COLLECTION must be true or false"
            )
        config = cls(
            user_header=os.getenv(
                "FINTRAFFIC_USER_HEADER", "webcam-iot-ingest"
            ).strip(),
            stations_url=os.getenv(
                "FINTRAFFIC_STATIONS_URL",
                "https://tie.digitraffic.fi/api/weathercam/v1/stations",
            ).strip(),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            retry_count=int(os.getenv("FINTRAFFIC_DISCOVERY_RETRY_COUNT", "2")),
            retry_backoff_s=float(
                os.getenv("FINTRAFFIC_DISCOVERY_RETRY_BACKOFF_S", "1")
            ),
            request_delay_s=float(
                os.getenv("FINTRAFFIC_DISCOVERY_REQUEST_DELAY_S", "0.1")
            ),
            selected_collection_status=os.getenv(
                "FINTRAFFIC_SELECTED_COLLECTION_STATUS", "GATHERING"
            ).strip(),
            require_in_collection=require_in_collection == "true",
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.user_header:
            raise ValueError("FINTRAFFIC_USER_HEADER cannot be empty")
        if "\r" in self.user_header or "\n" in self.user_header:
            raise ValueError("FINTRAFFIC_USER_HEADER cannot contain line breaks")
        if not self.stations_url.startswith("https://"):
            raise ValueError("Fintraffic stations URL must use HTTPS")
        if self.request_timeout_s <= 0:
            raise ValueError("provider request timeout must be positive")
        if self.retry_count < 0:
            raise ValueError("Fintraffic retry count cannot be negative")
        if self.retry_backoff_s < 0:
            raise ValueError("Fintraffic retry backoff cannot be negative")
        if self.request_delay_s < 0:
            raise ValueError("Fintraffic request delay cannot be negative")
        if not self.selected_collection_status:
            raise ValueError(
                "Fintraffic selected collection status cannot be empty"
            )


@dataclass(frozen=True)
class SkapingConfig:
    api_key_file: Path
    summary_url: str
    request_timeout_s: float
    retry_count: int
    retry_backoff_s: float
    request_delay_s: float
    minimum_camera_count: int
    member_countries: tuple[str, ...] = EUMETNET_MEMBER_COUNTRIES
    selected_rendition: str = "mini"

    @classmethod
    def from_environment(cls) -> "SkapingConfig":
        config = cls(
            api_key_file=Path(
                os.getenv("SKAPING_API_KEY_FILE", ".secrets/skaping_api_key")
            ),
            summary_url=os.getenv(
                "SKAPING_SUMMARY_URL",
                "https://api.skaping.com/camera/summaryApp",
            ).strip(),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            retry_count=int(os.getenv("SKAPING_DISCOVERY_RETRY_COUNT", "2")),
            retry_backoff_s=float(
                os.getenv("SKAPING_DISCOVERY_RETRY_BACKOFF_S", "1")
            ),
            request_delay_s=float(
                os.getenv("SKAPING_DISCOVERY_REQUEST_DELAY_S", "0")
            ),
            minimum_camera_count=int(
                os.getenv("SKAPING_DISCOVERY_MIN_CAMERAS", "20")
            ),
            member_countries=tuple(
                code.strip().upper()
                for code in os.getenv(
                    "SKAPING_MEMBER_COUNTRIES",
                    ",".join(EUMETNET_MEMBER_COUNTRIES),
                ).split(",")
                if code.strip()
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.summary_url.startswith("https://"):
            raise ValueError("Skaping summary URL must use HTTPS")
        if self.request_timeout_s <= 0:
            raise ValueError("provider request timeout must be positive")
        if self.retry_count < 0:
            raise ValueError("Skaping retry count cannot be negative")
        if self.retry_backoff_s < 0 or self.request_delay_s < 0:
            raise ValueError("Skaping request delays cannot be negative")
        if self.minimum_camera_count < 1:
            raise ValueError(
                "Skaping discovery minimum camera count must be positive"
            )
        if not self.member_countries or any(
            len(code) != 2 or not code.isalpha()
            for code in self.member_countries
        ):
            raise ValueError("Skaping member countries must be ISO alpha-2 codes")
        if self.selected_rendition != "mini":
            raise ValueError("Skaping discovery must select the mini rendition")

    def read_api_key(self) -> str:
        api_key = self.api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError(
                f"Skaping API key file is empty: {self.api_key_file}"
            )
        return api_key


@dataclass(frozen=True)
class WindyIngestionConfig:
    api_key_file: Path
    request_timeout_s: float
    image_download_timeout_s: float
    image_max_bytes: int
    request_delay_s: float
    minimum_ingestion_interval_s: float
    minimum_polling_interval_s: float
    polling_interval_factor: float
    freshness_query_retry_count: int
    download_retry_count: int
    retry_backoff_s: float
    ema_alpha: float
    initial_ema_seconds: float
    period_direct_replacement_modulus: int
    default_limit: int

    @classmethod
    def from_environment(cls) -> "WindyIngestionConfig":
        config = cls(
            api_key_file=Path(
                os.getenv("WINDY_API_KEY_FILE", ".secrets/windy_api_key")
            ),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            image_download_timeout_s=float(
                os.getenv("IMAGE_DOWNLOAD_TIMEOUT_S", "15")
            ),
            image_max_bytes=int(os.getenv("SOURCE_IMAGE_MAX_BYTES", "10000000")),
            minimum_ingestion_interval_s=float(
                os.getenv(
                    "WINDY_MINIMUM_INGESTION_INTERVAL_S",
                    os.getenv("MINIMUM_INGESTION_INTERVAL_S", "300"),
                )
            ),
            minimum_polling_interval_s=float(
                os.getenv("WINDY_MINIMUM_POLLING_INTERVAL_S", "540")
            ),
            polling_interval_factor=float(
                os.getenv(
                    "WINDY_POLLING_INTERVAL_FACTOR",
                    os.getenv("POLLING_INTERVAL_FACTOR", "0.7"),
                )
            ),
            request_delay_s=float(
                os.getenv("WINDY_INGESTION_REQUEST_DELAY_S", "0.1")
            ),
            freshness_query_retry_count=int(
                os.getenv("WINDY_FRESHNESS_QUERY_RETRY_COUNT", "0")
            ),
            download_retry_count=int(
                os.getenv("WINDY_DOWNLOAD_RETRY_COUNT", "0")
            ),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "1")),
            ema_alpha=float(os.getenv("EMA_ALPHA", "0.2")),
            initial_ema_seconds=float(
                os.getenv("WINDY_INITIAL_EMA_DOWNLOAD_PERIOD_S", "120")
            ),
            period_direct_replacement_modulus=int(
                os.getenv("WINDY_PERIOD_DIRECT_REPLACEMENT_MODULUS", "250")
            ),
            default_limit=int(os.getenv("WINDY_INGESTION_DEFAULT_LIMIT", "10")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.request_timeout_s <= 0 or self.image_download_timeout_s <= 0:
            raise ValueError("Windy ingestion timeouts must be positive")
        if self.image_max_bytes < 1:
            raise ValueError("source image maximum bytes must be positive")
        if self.minimum_ingestion_interval_s < 0:
            raise ValueError("minimum ingestion interval cannot be negative")
        if self.minimum_polling_interval_s < 0:
            raise ValueError("minimum polling interval cannot be negative")
        if self.initial_ema_seconds <= 0:
            raise ValueError("initial Windy EMA must be positive")
        if self.polling_interval_factor < 0:
            raise ValueError("polling interval factor cannot be negative")
        if self.request_delay_s < 0 or self.retry_backoff_s < 0:
            raise ValueError("Windy ingestion delays cannot be negative")
        if (
            self.freshness_query_retry_count < 0
            or self.download_retry_count < 0
        ):
            raise ValueError("Windy retry counts cannot be negative")
        if not 0 <= self.ema_alpha <= 1:
            raise ValueError("EMA alpha must be between 0 and 1")
        if self.default_limit < 1:
            raise ValueError("Windy ingestion default limit must be positive")
        if self.period_direct_replacement_modulus < 1:
            raise ValueError("Windy direct-replacement modulus must be positive")

    def read_api_key(self) -> str:
        api_key = self.api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError(f"Windy API key file is empty: {self.api_key_file}")
        return api_key


@dataclass(frozen=True)
class FintrafficIngestionConfig:
    user_header: str
    data_url: str
    image_base_url: str
    request_timeout_s: float
    image_download_timeout_s: float
    image_max_bytes: int
    request_delay_s: float
    minimum_ingestion_interval_s: float
    minimum_polling_interval_s: float
    polling_interval_factor: float
    freshness_query_retry_count: int
    download_retry_count: int
    retry_backoff_s: float
    ema_alpha: float
    period_direct_replacement_modulus: int
    default_limit: int

    @classmethod
    def from_environment(cls) -> "FintrafficIngestionConfig":
        config = cls(
            user_header=os.getenv(
                "FINTRAFFIC_USER_HEADER", "webcam-iot-ingest"
            ).strip(),
            data_url=os.getenv(
                "FINTRAFFIC_STATIONS_DATA_URL",
                "https://tie.digitraffic.fi/api/weathercam/v1/stations/data",
            ).strip(),
            image_base_url=os.getenv(
                "FINTRAFFIC_IMAGE_BASE_URL",
                "https://weathercam.digitraffic.fi",
            ).rstrip("/"),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            image_download_timeout_s=float(
                os.getenv("IMAGE_DOWNLOAD_TIMEOUT_S", "15")
            ),
            image_max_bytes=int(os.getenv("SOURCE_IMAGE_MAX_BYTES", "10000000")),
            request_delay_s=float(
                os.getenv("FINTRAFFIC_INGESTION_REQUEST_DELAY_S", "0.1")
            ),
            minimum_ingestion_interval_s=float(
                os.getenv("MINIMUM_INGESTION_INTERVAL_S", "300")
            ),
            minimum_polling_interval_s=float(
                os.getenv("FINTRAFFIC_MINIMUM_POLLING_INTERVAL_S", "480")
            ),
            polling_interval_factor=float(
                os.getenv("POLLING_INTERVAL_FACTOR", "0.7")
            ),
            freshness_query_retry_count=int(
                os.getenv("FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT", "0")
            ),
            download_retry_count=int(
                os.getenv("FINTRAFFIC_DOWNLOAD_RETRY_COUNT", "0")
            ),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "1")),
            ema_alpha=float(os.getenv("EMA_ALPHA", "0.2")),
            period_direct_replacement_modulus=int(
                os.getenv("FINTRAFFIC_PERIOD_DIRECT_REPLACEMENT_MODULUS", "250")
            ),
            default_limit=int(os.getenv("FINTRAFFIC_INGESTION_DEFAULT_LIMIT", "10")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.user_header or "\n" in self.user_header or "\r" in self.user_header:
            raise ValueError("invalid Fintraffic user header")
        if not self.data_url.startswith("https://") or not self.image_base_url.startswith(
            "https://"
        ):
            raise ValueError("Fintraffic ingestion URLs must use HTTPS")
        if self.request_timeout_s <= 0 or self.image_download_timeout_s <= 0:
            raise ValueError("Fintraffic ingestion timeouts must be positive")
        if self.image_max_bytes < 1 or self.minimum_ingestion_interval_s < 0:
            raise ValueError("invalid Fintraffic ingestion size or interval")
        if self.minimum_polling_interval_s < 0:
            raise ValueError("Fintraffic minimum polling interval cannot be negative")
        if (
            self.polling_interval_factor < 0
            or self.request_delay_s < 0
            or self.retry_backoff_s < 0
        ):
            raise ValueError("invalid Fintraffic polling or retry backoff")
        if self.freshness_query_retry_count < 0 or self.download_retry_count < 0:
            raise ValueError("Fintraffic retry counts cannot be negative")
        if not 0 <= self.ema_alpha <= 1 or self.default_limit < 1:
            raise ValueError("invalid Fintraffic EMA alpha or default limit")
        if self.period_direct_replacement_modulus < 1:
            raise ValueError("Fintraffic direct-replacement modulus must be positive")


@dataclass(frozen=True)
class SkapingIngestionConfig:
    api_key_file: Path
    request_timeout_s: float
    image_download_timeout_s: float
    image_max_bytes: int
    request_delay_s: float
    minimum_ingestion_interval_s: float
    minimum_polling_interval_s: float
    polling_interval_factor: float
    freshness_query_retry_count: int
    download_retry_count: int
    retry_backoff_s: float
    ema_alpha: float
    period_direct_replacement_modulus: int
    default_limit: int

    @classmethod
    def from_environment(cls) -> "SkapingIngestionConfig":
        config = cls(
            api_key_file=Path(
                os.getenv("SKAPING_API_KEY_FILE", ".secrets/skaping_api_key")
            ),
            request_timeout_s=float(os.getenv("PROVIDER_REQUEST_TIMEOUT_S", "15")),
            image_download_timeout_s=float(
                os.getenv("IMAGE_DOWNLOAD_TIMEOUT_S", "15")
            ),
            image_max_bytes=int(os.getenv("SOURCE_IMAGE_MAX_BYTES", "10000000")),
            request_delay_s=float(
                os.getenv("SKAPING_INGESTION_REQUEST_DELAY_S", "0.1")
            ),
            minimum_ingestion_interval_s=float(
                os.getenv("MINIMUM_INGESTION_INTERVAL_S", "300")
            ),
            minimum_polling_interval_s=float(
                os.getenv("SKAPING_MINIMUM_POLLING_INTERVAL_S", "240")
            ),
            polling_interval_factor=float(
                os.getenv("POLLING_INTERVAL_FACTOR", "0.7")
            ),
            freshness_query_retry_count=int(
                os.getenv("SKAPING_FRESHNESS_QUERY_RETRY_COUNT", "0")
            ),
            download_retry_count=int(
                os.getenv("SKAPING_DOWNLOAD_RETRY_COUNT", "0")
            ),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "1")),
            ema_alpha=float(os.getenv("EMA_ALPHA", "0.2")),
            period_direct_replacement_modulus=int(
                os.getenv("SKAPING_PERIOD_DIRECT_REPLACEMENT_MODULUS", "250")
            ),
            default_limit=int(os.getenv("SKAPING_INGESTION_DEFAULT_LIMIT", "10")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.request_timeout_s <= 0 or self.image_download_timeout_s <= 0:
            raise ValueError("Skaping ingestion timeouts must be positive")
        if self.image_max_bytes < 1 or self.minimum_ingestion_interval_s < 0:
            raise ValueError("invalid Skaping ingestion size or interval")
        if self.minimum_polling_interval_s < 0:
            raise ValueError("Skaping minimum polling interval cannot be negative")
        if (
            self.polling_interval_factor < 0
            or self.request_delay_s < 0
            or self.retry_backoff_s < 0
        ):
            raise ValueError("invalid Skaping polling or retry backoff")
        if self.freshness_query_retry_count < 0 or self.download_retry_count < 0:
            raise ValueError("Skaping retry counts cannot be negative")
        if not 0 <= self.ema_alpha <= 1 or self.default_limit < 1:
            raise ValueError("invalid Skaping EMA alpha or default limit")
        if self.period_direct_replacement_modulus < 1:
            raise ValueError("Skaping direct-replacement modulus must be positive")

    def read_api_key(self) -> str:
        api_key = self.api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ValueError(
                f"Skaping API key file is empty: {self.api_key_file}"
            )
        return api_key


@dataclass(frozen=True)
class TransformationConfig:
    version: str
    max_height_px: int
    jpeg_quality_initial: int
    target_size_bytes: int
    panoramic_target_size_bytes: int
    panoramic_aspect_ratio: float

    @classmethod
    def from_environment(cls) -> "TransformationConfig":
        config = cls(
            version=os.getenv("TRANSFORMATION_VERSION", "T0V0"),
            max_height_px=int(os.getenv("MAX_DERIVED_HEIGHT_PX", "288")),
            jpeg_quality_initial=int(os.getenv("JPEG_QUALITY_INITIAL", "90")),
            target_size_bytes=int(os.getenv("TARGET_IMAGE_SIZE_BYTES", "50000")),
            panoramic_target_size_bytes=int(
                os.getenv("TARGET_PANORAMIC_IMAGE_SIZE_BYTES", "200000")
            ),
            panoramic_aspect_ratio=float(
                os.getenv("PANORAMIC_ASPECT_RATIO_THRESHOLD", "2.0")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if len(self.version) != 4 or not self.version.isalnum():
            raise ValueError("transformation version must be four alphanumeric characters")
        if self.max_height_px < 1:
            raise ValueError("maximum derived height must be positive")
        if not 1 <= self.jpeg_quality_initial <= 95:
            raise ValueError("initial JPEG quality must be between 1 and 95")
        if self.target_size_bytes < 1 or self.panoramic_target_size_bytes < 1:
            raise ValueError("derived image size targets must be positive")
        if self.panoramic_aspect_ratio <= 1:
            raise ValueError("panoramic aspect ratio threshold must exceed one")


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    bucket: str
    prefix: str
    public_url_base: str
    region: str | None
    access_key_file: Path
    secret_key_file: Path
    retry_count: int
    retry_backoff_s: float
    addressing_style: str = "path"

    @classmethod
    def from_environment(cls) -> "S3Config":
        config = cls(
            endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
            bucket=os.getenv("S3_BUCKET", "webcam"),
            prefix=os.getenv("S3_PREFIX", "").strip("/"),
            public_url_base=os.getenv("S3_PUBLIC_URL_BASE", ""),
            region=os.getenv("S3_REGION") or None,
            access_key_file=Path(
                os.getenv("S3_ACCESS_KEY_FILE", ".secrets/s3_access_key")
            ),
            secret_key_file=Path(
                os.getenv("S3_SECRET_KEY_FILE", ".secrets/s3_secret_key")
            ),
            retry_count=int(os.getenv("S3_UPLOAD_RETRY_COUNT", "1")),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "1")),
            addressing_style=os.getenv("S3_ADDRESSING_STYLE", "path"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.endpoint_url.startswith(("http://", "https://")):
            raise ValueError("S3 endpoint URL must use HTTP or HTTPS")
        if not self.public_url_base.startswith(("http://", "https://")):
            raise ValueError("S3 public URL base must use HTTP or HTTPS")
        if not self.bucket or self.retry_count < 0 or self.retry_backoff_s < 0:
            raise ValueError("invalid S3 bucket or retry configuration")
        if self.addressing_style not in {"path", "virtual"}:
            raise ValueError("S3 addressing style must be path or virtual")

    def read_credentials(self) -> tuple[str, str]:
        access_key = self.access_key_file.read_text(encoding="utf-8").strip()
        secret_key = self.secret_key_file.read_text(encoding="utf-8").strip()
        if not access_key or not secret_key:
            raise ValueError("S3 credential files cannot be empty")
        return access_key, secret_key


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    tls_enabled: bool
    topic_prefix: str
    qos: int
    retry_count: int
    retry_backoff_s: float

    @classmethod
    def from_environment(cls) -> "MqttConfig":
        tls_value = os.getenv("MQTT_TLS", "false").lower()
        if tls_value not in {"true", "false"}:
            raise ValueError("MQTT_TLS must be true or false")
        config = cls(
            host=os.getenv("MQTT_HOST", "localhost"),
            port=int(os.getenv("MQTT_PORT", "1883")),
            tls_enabled=tls_value == "true",
            topic_prefix=os.getenv("MQTT_TOPIC_PREFIX", "webcam").strip("/"),
            qos=int(os.getenv("MQTT_QOS", "1")),
            retry_count=int(os.getenv("MQTT_PUBLICATION_RETRY_COUNT", "1")),
            retry_backoff_s=float(os.getenv("RETRY_BACKOFF_S", "1")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("invalid MQTT host or port")
        if not self.topic_prefix or self.qos not in {0, 1, 2}:
            raise ValueError("invalid MQTT topic prefix or QoS")
        if self.retry_count < 0 or self.retry_backoff_s < 0:
            raise ValueError("invalid MQTT retry configuration")


@dataclass(frozen=True)
class WorkerConfig:
    threads: int
    max_jobs_per_epoch: int
    idle_delay_s: float
    failure_backoff_s: float
    shutdown_grace_s: float
    health_host: str
    health_port: int
    database_pool_size: int = 16
    minimum_epoch_period_s: float = 15
    initial_stagger_window_s: float = 600
    readiness_window_s: float = 600

    @classmethod
    def from_environment(cls) -> "WorkerConfig":
        config = cls(
            threads=int(os.getenv("INGESTION_WORKER_THREADS", "4")),
            max_jobs_per_epoch=int(os.getenv("INGESTION_MAX_JOBS_PER_EPOCH", "100")),
            idle_delay_s=float(os.getenv("INGESTION_IDLE_DELAY_S", "5")),
            failure_backoff_s=float(os.getenv("INGESTION_FAILURE_BACKOFF_S", "10")),
            shutdown_grace_s=float(os.getenv("INGESTION_SHUTDOWN_GRACE_S", "30")),
            health_host=os.getenv("INGESTION_HEALTH_HOST", "127.0.0.1"),
            health_port=int(os.getenv("INGESTION_HEALTH_PORT", "8002")),
            database_pool_size=int(os.getenv("INGESTION_DATABASE_POOL_SIZE", "16")),
            minimum_epoch_period_s=float(
                os.getenv("INGESTION_MIN_EPOCH_PERIOD_S", "15")
            ),
            initial_stagger_window_s=float(
                os.getenv("INITIAL_STAGGER_WINDOW_S", "600")
            ),
            readiness_window_s=float(
                os.getenv("INGESTION_READINESS_WINDOW_S", "600")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not 1 <= self.threads <= 128:
            raise ValueError("worker threads must be between 1 and 128")
        if self.max_jobs_per_epoch < 1:
            raise ValueError("worker epoch limit must be positive")
        if not 1 <= self.database_pool_size <= 64:
            raise ValueError("database pool size must be between 1 and 64")
        if min(
            self.idle_delay_s,
            self.failure_backoff_s,
            self.shutdown_grace_s,
            self.minimum_epoch_period_s,
        ) < 0:
            raise ValueError("worker delays cannot be negative")
        if self.initial_stagger_window_s <= 0:
            raise ValueError("initial stagger window must be positive")
        if self.readiness_window_s <= 0:
            raise ValueError("worker readiness window must be positive")
        if not self.health_host or not 1 <= self.health_port <= 65535:
            raise ValueError("invalid worker health host or port")
