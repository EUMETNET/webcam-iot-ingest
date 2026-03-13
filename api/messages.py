import logging

logger = logging.getLogger(__name__)


def build_messages(payload: object, object_url, uploaded) -> object:
    # Remove the file content from the message and replace it with the S3 object URL and upload time
    message = payload.model_dump_json(
        exclude={"properties": {"content": {"file"}}}, exclude_none=True
    )
    message["properties"]["content"]["file"] = object_url
    message["properties"]["pubtime"] = uploaded

    return message
