import base64
import hashlib
import io

from PIL import Image
from PIL import UnidentifiedImageError
from typing import Annotated, Literal
from dateutil import parser

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator


class Coordinate(BaseModel):
    lat: float
    lon: float


class Geometry(BaseModel):
    type: Literal["Point"]
    coordinates: Coordinate


class Content(BaseModel):
    size: int | None = Field(
        None,
        description=("Number of bytes contained in the file."),
    )
    color_information: str | None = Field(
        None,
        description=("Information about the color encoding of the file"),
    )
    file: bytes = Field(
        ...,
        description=("Base64 encoded file content."),
    )

    @field_validator("file", mode="before")
    @classmethod
    def decode_base64(cls, v: object) -> bytes:
        if isinstance(v, str):
            try:
                return base64.b64decode(v)
            except Exception:
                raise ValueError("file is not valid base64")
        return v

    @model_validator(mode="after")
    def validate_and_convert_image(self) -> "Content":
        try:
            img = Image.open(io.BytesIO(self.file))
            img.load()  # force full decode to catch corruption
        except (UnidentifiedImageError, Exception) as e:
            raise ValueError(f"Image is corrupted or unrecognised: {e}")

        if img.format == "GIF":
            img.seek(0)  # use first frame
            img = img.convert("RGB")

        if img.width > 640 or img.height > 480:
            img.thumbnail((640, 480), Image.LANCZOS)

        if img.format != "JPEG":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        self.file = buf.getvalue()

        return self


class Properties(BaseModel):
    webcam_id: str | None = Field(
        None,
        description="Auto-generated unique identifier for the webcam, derived from its position and attributes.",
    )

    network: str = Field(
        ...,
        description=("The name of the network the image is associated with."),
    )

    location_name: str | None = Field(
        None,
        description=("The name of the location of the camera, e.g. a city."),
    )

    title: str | None = Field(
        None,
        description=("Short phrase or sentence describing the image."),
    )

    keywords: str | None = Field(
        None,
        description=("Keywords describing the scene."),
    )

    source: str | None = Field(
        None,
        description=("Any information of the camera model"),
    )

    direction: (
        Literal[
            "north",
            "east",
            "south",
            "west",
            "northeast",
            "northwest",
            "southeast",
            "southwest",
        ]
        | Annotated[int, Field(ge=0, le=359)]
        | None
    ) = Field(
        None,
        description=("Direction of the camera."),
    )

    altitude: int | float | None = Field(
        None,
        description=("The number of meters above mean sea level of the camera."),
    )

    image_datetime: str = Field(
        ...,
        description="Identifies the date/time of when the image being published was taken, in RFC3339 format.",
    )

    platform_datetime: str | None = Field(
        None,
        description="Identifies the date/time of when the image being published was updated on the platform, in RFC3339 format.",
    )

    content: Content = Field(..., description="Actual data content")

    # Prevents user from setting webcam_id
    @model_validator(mode="before")
    @classmethod
    def strip_webcam_id(cls, data: dict) -> dict:
        if isinstance(data, dict):
            data.pop("webcam_id", None)
        return data

    # Checks that image_datetime and platform_datetime (if provided) are in ISO 8601 format and in UTC timezone
    @model_validator(mode="after")
    def check_datetime_iso(self) -> "Properties":
        try:
            dt = parser.isoparse(self.image_datetime)
        except ValueError:
            raise ValueError(
                f"{self.image_datetime} not in ISO format(YYYY-MM-DDTHH:MM:SSZ)"
            )
        except Exception as e:
            raise e

        if dt.tzname() != "UTC":
            raise ValueError(
                f"Input datetime, {self.image_datetime}, is not in UTC timezone"
            )

        if self.platform_datetime is not None:
            try:
                pdt = parser.isoparse(self.platform_datetime)
            except ValueError:
                raise ValueError(
                    f"{self.platform_datetime} not in ISO format(YYYY-MM-DDTHH:MM:SSZ)"
                )
            except Exception as e:
                raise e

            if pdt.tzname() != "UTC":
                raise ValueError(
                    f"Input platform_datetime, {self.platform_datetime}, is not in UTC timezone"
                )

        return self


class FileUpload(BaseModel):
    type: Literal["Feature"]
    geometry: Geometry
    properties: Properties

    @model_validator(mode="after")
    def generate_webcam_id(self) -> "FileUpload":
        components = [
            str(self.geometry.coordinates.lat),
            str(self.geometry.coordinates.lon),
            str(self.properties.direction)
            if self.properties.direction is not None
            else "",
            self.properties.source or "",
        ]
        raw = ":".join(components).encode()
        self.properties.webcam_id = hashlib.sha256(raw).hexdigest()[:16]
        return self
