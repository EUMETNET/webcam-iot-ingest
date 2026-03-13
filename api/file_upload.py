import logging
import os

import aioboto3
from botocore.exceptions import ClientError
from botocore.config import Config
from fastapi import File

BUCKET_NAME = os.getenv("BUCKET_NAME", "webcam")
BUCKET_ACCESS_KEY_ID = os.getenv("BUCKET_ACCESS_KEY_ID")
BUCKET_SECRET_ACCESS_KEY = os.getenv("BUCKET_SECRET_ACCESS_KEY")
BUCKET_ENDPOINT_URL = os.getenv("BUCKET_ENDPOINT_URL")
BUCKET_OBJECT_URL = os.getenv(
    "BUCKET_OBJECT_URL", f"{BUCKET_ENDPOINT_URL}/{BUCKET_NAME}"
)

logger = logging.getLogger(__name__)


async def upload_fileobject(file_name: str, file_object: File) -> str | None:
    """Upload a file to an S3 bucket and return the public URL of the uploaded file.

    Retention in the bucket is controlled by the bucket's lifecycle policy,
    which should be configured to delete objects after a set number of days.

    :param file_name: S3 object key
    :param file_object: File-like object to upload
    :param delete_after_24h: Whether to set the object to expire after 24 hours
    :return: Public object URL on success, None on failure
    """

    session = aioboto3.Session()
    async with session.client(
        "s3",
        aws_access_key_id=BUCKET_ACCESS_KEY_ID,
        aws_secret_access_key=BUCKET_SECRET_ACCESS_KEY,
        endpoint_url=BUCKET_ENDPOINT_URL,
    ) as s3_client:
        try:
            await s3_client.upload_fileobj(
                file_object,
                BUCKET_NAME,
                file_name,
            )
            object_url = f"{BUCKET_OBJECT_URL}/{file_name}"
            logger.debug(f"Uploaded {file_name} to {BUCKET_NAME}, url={object_url}")
        except ClientError as e:
            logger.error(e)
            return None
        return object_url
