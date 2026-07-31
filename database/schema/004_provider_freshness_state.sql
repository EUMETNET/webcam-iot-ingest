BEGIN;

ALTER TABLE source_stream
    ADD COLUMN IF NOT EXISTS last_provider_update_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS last_observed_provider_image_marker text,
    ADD COLUMN IF NOT EXISTS last_processed_provider_update_timestamp timestamptz;

-- Existing Windy and Fintraffic markers were timestamp-shaped freshness
-- values. Preserve them as observed and processed provider timestamps.
UPDATE source_stream AS ss
SET last_provider_update_timestamp = ss.last_provider_image_marker::timestamptz,
    last_processed_provider_update_timestamp =
        ss.last_provider_image_marker::timestamptz,
    last_provider_image_marker = NULL
FROM site AS s
WHERE ss.site_id = s.site_id
  AND s.network_id IN ('win', 'fin')
  AND ss.last_provider_image_marker IS NOT NULL
  AND pg_input_is_valid(
      ss.last_provider_image_marker,
      'timestamp with time zone'
  );

-- Existing Skaping markers are opaque ETags. They were successfully handled
-- markers, so they are also the best initial observed-marker value.
UPDATE source_stream AS ss
SET last_observed_provider_image_marker = ss.last_provider_image_marker
FROM site AS s
WHERE ss.site_id = s.site_id
  AND s.network_id = 'ska'
  AND ss.last_provider_image_marker IS NOT NULL
  AND ss.last_observed_provider_image_marker IS NULL;

COMMIT;
