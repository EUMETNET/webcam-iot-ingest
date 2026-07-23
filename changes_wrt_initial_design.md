# Changes with respect to the initial design

## Purpose and maintenance rule

This file is the repository-level register of deliberate deviations,
clarifications, provider-specific choices, and material implementation
refinements relative to the initial ingestion architecture.

The architecture document remains private, read-only background material and is
not reproduced here. This register summarizes only the differences needed to
understand the public implementation.

From 2026-07-23 onward, append a dated entry whenever implementation or live
validation reveals another material difference. Do not silently rewrite an old
decision if its history matters: mark it superseded and add the replacement.

Terminology used below:

- **Deviation**: implemented behavior differs from the initial default.
- **Choice**: the initial design left alternatives or a configurable value
  open, and the pilot selected one.
- **Refinement**: additional behavior preserves the intended contract.
- **Deferred**: intended architecture work is assigned to a later checkpoint.

## Consolidated register as of 2026-07-23

### Checkpoint sequence and altitude enrichment

**Type:** refinement to the implementation sequence.

Altitude enrichment became checkpoint 4, before image acquisition. The
previous checkpoints numbered 4 and above were shifted by one. Altitude is
implemented as a provider-independent, resumable database completion pass and
is also invoked by discovery workflows.

The elevation service has a daily request allowance, so one discovery does not
attempt multiple same-day passes merely to fill every null altitude. Missing
values remain null and eligible on subsequent daily runs. This gives eventual
completion over several days rather than making discovery fail when the daily
allowance or a provider response is incomplete.

Reliable provider altitude is preserved. Fintraffic's observed geometry
altitude `0.0` is retained only in raw metadata and is not accepted as site
altitude.

### Windy discovery coverage

**Type:** provider-specific choice and optimization.

The initial design described configurable overlapping geographic searches. The
implemented European discovery first obtains the country totals for all 33
EUMETNET members:

- countries at or below Windy's 1,000-result listing cap use complete country
  pagination;
- countries above the cap use overlapping geographic discs;
- current committed coverage uses 40 km discs for AT, CH, CZ, DE, ES, GB, IT,
  and NO, and 50 km discs for FI and FR;
- overlapping results are deduplicated by Windy webcam identifier.

This hybrid approach substantially reduces daily API calls while retaining
disc coverage where a country listing cannot be complete. The exact geographic
scope remains determined by the configured discs and Windy's country
classification; a country code alone is not treated as proof that every
overseas territory is geographically covered.

### Windy provider status

**Type:** deliberate deviation.

Windy's provider `status` field is stored unchanged in source-stream JSONB but
is not used to activate or inactivate registry streams. Consecutive live
snapshots showed recent working cameras fluctuating in that field. Registry
eligibility instead follows membership in the complete discovery snapshot,
manual blacklisting, and the shared reconciliation rules.

One complete-snapshot absence still marks a stream inactive. A proposed
Windy-specific grace/intermediate status for cameras that repeatedly disappear
and return has not been implemented.

### Windy site colocation

**Type:** configured pilot choice.

The distance under which two newly discovered Windy cameras may share an
inferred site is 10 metres. Existing site assignments remain stable even if a
later proximity calculation would choose differently. The 10 m threshold is
intentionally conservative because source-stream coordinates must remain
available independently in provider metadata even when streams share a site.

### Fintraffic discovery details and request header

**Type:** provider correction and refinement.

The live official application-identification header is
`Digitraffic-User`, not the older `Fintraffic-User` spelling used in the
initial background material.

The compact station list is authoritative for station/preset membership, but
its preset objects do not contain all required descriptive metadata. Discovery
therefore makes one paced detail request for every eligible station and treats
missing or malformed detail as an incomplete snapshot. `presentationName`,
direction, resolution, image URL, purpose, camera type, collection interval,
municipality, province, and road address are consequently retained during the
same discovery checkpoint rather than deferred.

Fintraffic sites use country `FI` directly after validation because the
provider is national. The generic coordinate-to-country lookup is not needed
for these sites.

### Per-stream polling eligibility

**Type:** deliberate deviation and experimental scheduling refinement.

The initial worker model made a stream due after its minimum interval since the
last successful download, with EMA primarily intended for monitoring. The
implemented due query uses two conditions for streams with successful state:

1. current time is at least
   `last_download_timestamp + minimum_ingestion_interval`; and
2. current time is at least
   `last_provider_image_marker + 0.7 * ema_download_period`, when the marker is
   a usable timestamp and EMA exists.

The current minimum ingestion interval is 300 seconds. The earlier proposed
formula `min(max(minimum interval, 0.7 * EMA), 30 minutes)` is not the
implemented formula; it was replaced by the conjunction above.

`last_freshness_query_timestamp` is recorded for applied provider checks but is
deliberately not a third due-selection guard. A never-successful stream, an
unchanged stream after its two guards pass, or a stream with repeated provider
errors can therefore be selected again in the next epoch. This behavior was
chosen for measurement and may be revisited if failed cameras generate
excessive load.

### Epoch timing

**Type:** operational refinement.

The per-camera 300-second ingestion interval is separate from epoch timing.
Epoch starts have a default minimum start-to-start separation of 15 seconds,
with no additional benchmark idle delay. There is no five-minute mandatory
pause between epochs.

Duration-bounded runs stop starting epochs after their deadline but allow the
active epoch to finish. Bounded epoch counts and duration-based `just` recipes
are validation conveniences not required by the initial service contract.

### Initial polling stagger

**Type:** benchmark and cold-start refinement.

An optional deterministic initial phase spreads the first check of each stream
over 600 seconds. Windy and Fintraffic use distinct fixed seeds. The phase is
derived from the source-stream identifier, is reproducible, is kept only in
worker memory, and does not fabricate database timestamps.

The phase is independent of EMA. EMA candidates from a stream's staggered
first check are discarded to avoid treating the artificial cold-start phase as
provider behavior.

### EMA update timing

**Type:** deliberate deviation.

Successful download state and durable publication state are written
immediately. EMA alone is deferred to the epoch coordinator and applied as a
conditional batch when:

- the epoch is not epoch 1; and
- the epoch duration is shorter than the 300-second minimum ingestion
  interval.

Long epochs discard their EMA candidates. Conditional database updates ensure
that a stale epoch cannot overwrite a more recent successful download. This
differs from updating EMA inside every successful source job.

### Retry defaults and separation

**Type:** provider-specific choice.

Retry controls are separated by provider and operation:

- transformation retries: 0;
- Windy freshness-query retries: 0;
- Windy image-download retries: 0;
- Fintraffic freshness-snapshot retries: 0;
- Fintraffic image-download retries: 0;
- S3 upload retries: 1;
- MQTT publication retries: 1.

Windy retry experiments showed fewer provider errors but a worse latency tail,
so its operational defaults returned to zero. Fintraffic also starts at zero;
metadata/image races are left for the next epoch.

### Fintraffic freshness detection

**Type:** major deliberate provider-specific choice.

The initial preferred Fintraffic path used one conditional JPEG request per
preset with ETag/`If-None-Match`. The implementation instead performs one bulk
station-data request per non-empty epoch and uses each preset's
`measuredTime` as:

- the opaque provider image marker; and
- the provider update timestamp used for latency metrics.

Only presets whose marker differs from the stored marker have their full JPEG
downloaded. This avoids one metadata/conditional request per camera and makes
the provider timestamp available without a second access path. ETag is not a
freshness dependency.

The downloaded JPEG's `Last-Modified` must match the snapshot marker. A
mismatch is treated as a metadata/image race and deferred to a later epoch.
JPEG responses without a verifiable `Last-Modified` are rejected. Live
inspection showed that stale Fintraffic cameras can return a valid JPEG
containing a grey "Image not available" placeholder without that header, so
the strict check also prevents provider placeholders from entering S3.

### Provider worker structure

**Type:** implementation refinement within the intended model.

Windy and Fintraffic run as separate long-lived workers with separate health
ports and Prometheus targets, while sharing transformation, publication,
database, and metric components. Their provider access loops are not forced
into an identical request structure: Windy performs per-camera freshness
requests, whereas Fintraffic refreshes one bulk metadata snapshot per epoch.

Benchmark recipes currently use up to 100 job threads and a bounded PostgreSQL
connection pool. These are measured pilot values, not fixed architectural
requirements. A future multi-VM or multi-worker partition remains possible.

### Durable S3/MQTT publication outbox

**Type:** material reliability refinement.

A `publication_outbox` table extends the initial registry state. A successful
download, immutable image identity, derived bytes, and notification are
recorded atomically before external delivery. Rows progress through
`pending_s3` and `pending_mqtt`; completion removes the row. Workers drain only
their own network's pending rows before selecting new work.

This permits restart recovery without downloading or transforming the source
image again and ensures MQTT is attempted only after S3 has succeeded.

### S3 immutable-write compatibility

**Type:** EWC-specific implementation choice.

The implementation does not rely on S3 `IfNoneMatch`, whose compatibility with
the EWC object store was uncertain. Before PUT it performs HEAD:

- a missing object is uploaded with SHA-256 metadata;
- an existing object is accepted only if content length and SHA-256 metadata
  match;
- differing content under the same immutable key is rejected.

This is a restart/idempotency safeguard. Normal concurrent ownership is handled
by the database outbox.

### Shutdown behavior

**Type:** implementation clarification.

SIGINT and SIGTERM stop new epoch intake and allow the active epoch to finish.
The configured shutdown-grace value exists for deployment policy, but the
current foreground worker does not forcibly terminate in-flight jobs when that
duration expires. Forceful service termination, if required, belongs to the
later systemd orchestration policy.

### Observability beyond the initial minimum

**Type:** refinement.

In addition to the originally requested outcomes and durations, both provider
workers expose:

- provider update to successful download latency;
- successful download to completed MQTT latency;
- provider update to completed MQTT latency;
- provider rate-gate wait separately from provider HTTP and complete refresh;
- source-job duration and controlled failure-reason codes;
- source and derived size, width, height, format, color mode, and color depth;
- P50, P90, and P95 dashboard estimates, including explicit P90 width and
  image size;
- completed external source-image payload bytes;
- bytes actually transferred by successful S3 PUT operations;
- one-minute-smoothed external-download and S3 payload throughput.

S3 byte throughput excludes an immutable object that was reused after a
matching HEAD because no PUT payload was transferred. Source-download byte
throughput counts completed image responses and excludes metadata requests,
failed/partial transfers, and protocol overhead.

Metrics use a bounded `source_network` label. Windy dashboard queries are
explicitly restricted to `source_network="win"` and Fintraffic queries to
`source_network="fin"` even though both are displayed by the same Grafana
instance and tunnel.

### Host and container addressing in validation commands

**Type:** operational choice.

One-shot `just ingest-fintraffic` runs Python on the host, so it connects to the
host-published Mosquitto listener at `127.0.0.1`. Containerized services may
use the Docker-internal hostname `mqtt`. Prometheus reaches host benchmark
workers through `host.docker.internal` on separate ports.

### Deferred orchestration and infrastructure health

**Type:** deferred initial-design work.

Checkpoint 12 owns production systemd services/timers, scheduled sequential
discovery, S3 cleanup, PostgreSQL backup/recovery drills, and standard
infrastructure health scraping. PostgreSQL, host, and container exporters
(`postgres_exporter`, `node_exporter`, and cAdvisor), infrastructure dashboards,
and alerts are not yet implemented.

Application worker health endpoints, worker metrics, Prometheus self-scraping,
Grafana dashboards, and MQTT exporter integration exist before checkpoint 12.

### Skaping status

**Type:** deferred provider implementation.

Skaping discovery and ingestion remain later checkpoints. The unresolved
choice between archive metadata and a validated latest-media redirect remains
open; no implementation decision should be inferred from the Windy or
Fintraffic worker.

## Append-only change log

### 2026-07-23 — Register created

Created this consolidated register after checkpoints 3–9 implementation and
live validation. Future material changes must be appended below with the date,
affected component, old behavior or assumption, new behavior, rationale, and
operational consequences.

### 2026-07-23 — Skaping discovery snapshot safeguards and country handling

**Affected component:** Skaping discovery (checkpoint 10).

The Skaping summary response is accepted as either its camera array or one of
the explicitly tested `cameras`/`data` envelopes. A malformed response, or a
camera count below `SKAPING_DISCOVERY_MIN_CAMERAS`, aborts before registry
reconciliation. After the first complete live response returned 45 cameras,
the default threshold was set to 20: below the observed total but high enough
to reject a materially truncated snapshot. This guards against an apparently
successful empty or partial response inactivating stored Skaping streams.

EUMETNET filtering is applied when a camera supplies a valid ISO alpha-2
country code. A camera with no provider country code is retained rather than
discarded from the authorized snapshot; its registry country remains null
unless an existing unchanged site already has a country. This avoids inferring
geography from coordinates during checkpoint 10 while preserving the complete
authorized provider snapshot.
