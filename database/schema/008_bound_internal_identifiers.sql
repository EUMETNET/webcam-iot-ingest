-- Historical compatibility migration. Identifier length is no longer bounded,
-- so this step enforces only the alphanumeric portion of the former contract.
-- Migration 009 replaces these legacy constraint names.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'site'::regclass
          AND conname = 'site_id_compact_alphanumeric'
    ) THEN
        ALTER TABLE site
            ADD CONSTRAINT site_id_compact_alphanumeric CHECK (
                site_id ~ '^[A-Za-z0-9]+$'
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
                source_stream_id ~ '^[A-Za-z0-9]+$'
            );
    END IF;
END
$$;
