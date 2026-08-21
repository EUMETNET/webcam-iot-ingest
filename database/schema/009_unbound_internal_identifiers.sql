-- Permit provider-derived identifiers to retain their complete sanitized form.
-- Alphanumeric validation remains enforced; only the former length bound is
-- removed.
ALTER TABLE site
    DROP CONSTRAINT IF EXISTS site_id_compact_alphanumeric,
    DROP CONSTRAINT IF EXISTS site_id_alphanumeric,
    ADD CONSTRAINT site_id_alphanumeric CHECK (
        site_id ~ '^[A-Za-z0-9]+$'
    );

ALTER TABLE source_stream
    DROP CONSTRAINT IF EXISTS source_stream_id_compact_alphanumeric,
    DROP CONSTRAINT IF EXISTS source_stream_id_alphanumeric,
    ADD CONSTRAINT source_stream_id_alphanumeric CHECK (
        source_stream_id ~ '^[A-Za-z0-9]+$'
    );
