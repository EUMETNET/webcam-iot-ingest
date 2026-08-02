-- Give the learned source-stream period an algorithm-neutral name while
-- preserving every existing value. This migration is idempotent for both
-- upgraded and newly initialized databases.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'source_stream'
          AND column_name = 'ema_download_period'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'source_stream'
          AND column_name = 'estimated_source_stream_period'
    ) THEN
        ALTER TABLE source_stream
            RENAME COLUMN ema_download_period
            TO estimated_source_stream_period;
    END IF;
END
$$;

ALTER TABLE source_stream
    DROP CONSTRAINT IF EXISTS source_stream_ema_nonnegative;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'source_stream'::regclass
          AND conname = 'source_stream_estimated_period_nonnegative'
    ) THEN
        ALTER TABLE source_stream
            ADD CONSTRAINT source_stream_estimated_period_nonnegative CHECK (
                estimated_source_stream_period IS NULL
                OR estimated_source_stream_period >= 0
            );
    END IF;
END
$$;
