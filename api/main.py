import datetime
import io
import logging
import os

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel

from api.api_metrics import add_metrics
from api.file_upload import upload_fileobject
from api.model import FileUpload
from api.send_mqtt import connect_mqtt
from api.send_mqtt import send_message
from api.messages import build_messages

log_level = os.environ.get("INGEST_LOGLEVEL", "INFO")

formatter = logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")
stream_handler = logging.StreamHandler()
stream_handler.setLevel(log_level)
stream_handler.setFormatter(formatter)

# Set logging level and handlers
logging.basicConfig(level=log_level, handlers=[stream_handler])
logger = logging.getLogger(__name__)


class Response(BaseModel):
    status_message: str
    status_code: int


# Define configuration parameters
mqtt_configuration = {
    "host": os.getenv("MQTT_HOST", "localhost"),
    "username": os.getenv("MQTT_USERNAME"),
    "password": os.getenv("MQTT_PASSWORD"),
    "enable_tls": os.getenv("MQTT_TLS", "False").lower() in ("true", "1", "t"),
    "port": int(os.getenv("MQTT_PORT", 1883)),
}


mqtt_client = connect_mqtt(mqtt_configuration)

app = FastAPI()
add_metrics(app)


@app.post("/upload", tags=["Upload"])
async def upload_file(payload: FileUpload):
    """
    Uploads a file to S3 and sends a message to the MQTT broker with the file S3 URL and metadata.
    """
    file_bytes = payload.properties.content.file
    file_obj = io.BytesIO(file_bytes)
    uploaded = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    filename = f"{uploaded}-{payload.properties.webcam_id}.jpg"

    object_url = await upload_fileobject(filename, file_obj)

    if object_url:
        topic = f"{payload.properties.network}/{payload.properties.webcam_id}"
        message = build_messages(payload, object_url, uploaded)

        send_message(topic, message, mqtt_client)
        return {
            "object_url": object_url,
            "uploaded": uploaded,
        }
    raise HTTPException(
        status_code=500,
        detail={"msg": "File upload failed", "reason": "Unable to reach bucket"},
    )
