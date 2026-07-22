ALTER TABLE source_stream
    ADD COLUMN IF NOT EXISTS last_freshness_query_timestamp timestamptz;

DROP INDEX IF EXISTS source_stream_due_idx;

CREATE INDEX source_stream_due_idx
    ON source_stream (status, last_freshness_query_timestamp)
    WHERE status = 'active';
