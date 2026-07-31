-- Keep one atomic freshness snapshot per download decision and one timestamp
-- for the latest image that completed MQTT publication.
ALTER TABLE source_stream
    ADD COLUMN IF NOT EXISTS last_observed_provider_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS last_observed_image_marker text,
    ADD COLUMN IF NOT EXISTS last_processed_timestamp timestamptz;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'source_stream'
          AND column_name = 'last_provider_update_timestamp'
    ) THEN
        EXECUTE 'UPDATE source_stream
                 SET last_observed_provider_timestamp = COALESCE(
                     last_observed_provider_timestamp,
                     last_provider_update_timestamp
                 )';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'source_stream'
          AND column_name = 'last_observed_provider_image_marker'
    ) THEN
        EXECUTE 'UPDATE source_stream
                 SET last_observed_image_marker = COALESCE(
                     last_observed_image_marker,
                     last_observed_provider_image_marker
                 )';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'source_stream'
          AND column_name = 'last_provider_image_marker'
    ) THEN
        EXECUTE 'UPDATE source_stream
                 SET last_observed_image_marker = COALESCE(
                     last_observed_image_marker,
                     last_provider_image_marker
                 )';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'source_stream'
          AND column_name = 'last_processed_provider_update_timestamp'
    ) THEN
        EXECUTE 'UPDATE source_stream
                 SET last_processed_timestamp = COALESCE(
                     last_processed_timestamp,
                     last_download_timestamp
                 )';
    END IF;
END
$$;

DROP INDEX IF EXISTS source_stream_due_idx;

ALTER TABLE source_stream
    DROP COLUMN IF EXISTS last_freshness_query_timestamp,
    DROP COLUMN IF EXISTS last_provider_image_marker,
    DROP COLUMN IF EXISTS last_provider_update_timestamp,
    DROP COLUMN IF EXISTS last_observed_provider_image_marker,
    DROP COLUMN IF EXISTS last_processed_provider_update_timestamp;

CREATE INDEX source_stream_due_idx
    ON source_stream (
        status,
        last_processed_timestamp,
        last_observed_provider_timestamp
    )
    WHERE status = 'active';
