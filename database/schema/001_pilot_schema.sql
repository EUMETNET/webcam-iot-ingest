BEGIN;

CREATE TABLE IF NOT EXISTS network (
    network_id text PRIMARY KEY,
    network_name text NOT NULL,
    api_version text,
    accessible_from timestamptz,
    accessible_until timestamptz,
    CONSTRAINT network_id_not_empty CHECK (network_id <> ''),
    CONSTRAINT network_access_window_valid CHECK (
        accessible_from IS NULL
        OR accessible_until IS NULL
        OR accessible_from <= accessible_until
    )
);

CREATE TABLE IF NOT EXISTS site (
    site_id text PRIMARY KEY,
    network_id text NOT NULL REFERENCES network(network_id),
    provider_site_id text,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    altitude double precision,
    country text,
    corrected_latitude double precision,
    corrected_longitude double precision,
    corrected_altitude double precision,
    provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT site_id_not_empty CHECK (site_id <> ''),
    CONSTRAINT site_latitude_valid CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT site_longitude_valid CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT site_corrected_latitude_valid CHECK (
        corrected_latitude IS NULL OR corrected_latitude BETWEEN -90 AND 90
    ),
    CONSTRAINT site_corrected_longitude_valid CHECK (
        corrected_longitude IS NULL OR corrected_longitude BETWEEN -180 AND 180
    ),
    CONSTRAINT site_country_valid CHECK (country IS NULL OR country ~ '^[A-Z]{2}$'),
    CONSTRAINT site_provider_metadata_is_object CHECK (
        jsonb_typeof(provider_metadata) = 'object'
    )
);

CREATE TABLE IF NOT EXISTS source_stream (
    source_stream_id text PRIMARY KEY,
    site_id text NOT NULL REFERENCES site(site_id),
    provider_source_stream_id text NOT NULL,
    selected_rendition text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    provider_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_freshness_query_timestamp timestamptz,
    last_download_timestamp timestamptz,
    last_provider_image_marker text,
    ema_download_period double precision,
    CONSTRAINT source_stream_id_not_empty CHECK (source_stream_id <> ''),
    CONSTRAINT source_stream_provider_id_not_empty CHECK (provider_source_stream_id <> ''),
    CONSTRAINT source_stream_rendition_not_empty CHECK (selected_rendition <> ''),
    CONSTRAINT source_stream_status_valid CHECK (
        status IN ('active', 'inactive', 'blacklisted')
    ),
    CONSTRAINT source_stream_provider_metadata_is_object CHECK (
        jsonb_typeof(provider_metadata) = 'object'
    ),
    CONSTRAINT source_stream_ema_nonnegative CHECK (
        ema_download_period IS NULL OR ema_download_period >= 0
    ),
    CONSTRAINT source_stream_provider_id_per_site_unique UNIQUE (
        site_id,
        provider_source_stream_id
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS site_provider_id_per_network_unique
    ON site (network_id, provider_site_id)
    WHERE provider_site_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS site_network_id_idx ON site (network_id);
CREATE INDEX IF NOT EXISTS source_stream_site_id_idx ON source_stream (site_id);
CREATE INDEX IF NOT EXISTS source_stream_due_idx
    ON source_stream (status, last_freshness_query_timestamp)
    WHERE status = 'active';

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

INSERT INTO network (network_id, network_name, api_version)
VALUES
    ('win', 'Windy', 'v3'),
    ('fin', 'Fintraffic', 'v1'),
    ('ska', 'Skaping', NULL)
ON CONFLICT (network_id) DO UPDATE
SET network_name = EXCLUDED.network_name,
    api_version = EXCLUDED.api_version;

COMMIT;
