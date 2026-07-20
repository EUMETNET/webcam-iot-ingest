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
    request_delay_s: float = 1.0
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
                os.getenv("WINDY_SITE_DISTANCE_THRESHOLD_M", "100")
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
