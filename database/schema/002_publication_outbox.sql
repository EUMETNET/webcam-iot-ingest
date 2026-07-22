BEGIN;

CREATE TABLE IF NOT EXISTS publication_outbox (
    image_id text PRIMARY KEY,
    source_stream_id text NOT NULL REFERENCES source_stream(source_stream_id),
    provider_marker text NOT NULL,
    download_timestamp timestamptz NOT NULL,
    object_key text NOT NULL UNIQUE,
    derived_content bytea NOT NULL,
    notification jsonb NOT NULL,
    stage text NOT NULL DEFAULT 'pending_s3',
    attempt_count integer NOT NULL DEFAULT 0,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT publication_outbox_image_id_not_empty CHECK (image_id <> ''),
    CONSTRAINT publication_outbox_content_not_empty CHECK (octet_length(derived_content) > 0),
    CONSTRAINT publication_outbox_notification_object CHECK (jsonb_typeof(notification) = 'object'),
    CONSTRAINT publication_outbox_stage_valid CHECK (stage IN ('pending_s3', 'pending_mqtt')),
    CONSTRAINT publication_outbox_attempt_nonnegative CHECK (attempt_count >= 0)
);

CREATE INDEX IF NOT EXISTS publication_outbox_order_idx
    ON publication_outbox (created_at, image_id);

COMMIT;
