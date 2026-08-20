-- Enforce the compact internal identifier contract after provider-specific
-- discovery identifiers have been migrated or rediscovered.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'site'::regclass
          AND conname = 'site_id_compact_alphanumeric'
    ) THEN
        ALTER TABLE site
            ADD CONSTRAINT site_id_compact_alphanumeric CHECK (
                char_length(site_id) <= 16
                AND site_id ~ '^[A-Za-z0-9]+$'
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'source_stream'::regclass
          AND conname = 'source_stream_id_compact_alphanumeric'
    ) THEN
        ALTER TABLE source_stream
            ADD CONSTRAINT source_stream_id_compact_alphanumeric CHECK (
                char_length(source_stream_id) <= 16
                AND source_stream_id ~ '^[A-Za-z0-9]+$'
            );
    END IF;
END
$$;
