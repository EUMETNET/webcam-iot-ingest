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

The implemented due query uses two conditions:

1. current time is at least
   `last_processed_timestamp + minimum_ingestion_interval`; and
2. current time is at least
   `last_observed_provider_timestamp + 0.7 * estimated_source_stream_period`, when an
   observed timestamp and EMA exist.

The current minimum ingestion interval is 300 seconds. The earlier proposed
formula `min(max(minimum interval, 0.7 * EMA), 30 minutes)` is not the
implemented formula; it was replaced by the conjunction above.

Null state makes its corresponding guard pass. Provider results that do not
lead to a download decision are not persisted. This prevents timestamp and
marker components of one freshness snapshot from being acknowledged in
different epochs. `last_download_timestamp` records the local download
decision/attempt time but is not itself a due-selection guard.

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

Download-decision state, successful-publication state, and EMA candidates are
returned by source jobs and written together by the epoch coordinator. EMA is
applied only when:

- the epoch is not epoch 1; and
- the epoch duration is shorter than the 300-second minimum ingestion
  interval.

Long epochs and epoch 1 discard their EMA candidates while retaining the other
download-decision state. Initial stagger releases also discard their EMA
candidates. No source thread writes ingestion state individually.

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
station-data request per non-empty epoch and uses each preset's `measuredTime`
as its provider update timestamp. The JPEG ETag becomes available only during
the full GET. If that ETag equals the last observed marker, the downloaded
body is discarded; download-decision timestamp, marker, provider timestamp,
and eligible EMA are still retained, while processed state remains unchanged.

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

### 2026-07-23 — Discovery observability starts with Skaping

**Affected component:** discovery observability and checkpoint sequencing.

Application-level discovery observability was originally deferred with the
broader checkpoint-12 orchestration work. It is now implemented first for
Skaping during checkpoint 10, then will be applied through the same shared
functions to Fintraffic and finally Windy after the parallel ingestion test.
Checkpoint 12 retains systemd supervision, infrastructure exporters, health
alerts, backup/cleanup metrics, and production orchestration.

Skaping discovery is a sub-second service-level batch job, so it normally exits
before Prometheus's 15-second pull interval can scrape it. A pinned Prometheus
Pushgateway persists the batch metrics. Event counters/histograms are added
across runs, while current registry status and last-success gauges are
replaced. The fixed grouping key is only `source_network`; process/host labels
are deliberately omitted. Prometheus scrapes the gateway and a dedicated
Skaping discovery Grafana dashboard filters every query to
`source_network="ska"`.

Metrics publication is non-fatal. A committed registry update is not reported
as a discovery failure merely because monitoring is unavailable; the command
instead emits `discovery_metrics_published=false`. This preserves transaction
semantics while making the observability failure explicit in structured
output.

### 2026-07-23 — Fintraffic one-hour benchmark presentation

**Affected component:** ingestion performance reporting.

The completed one-hour Fintraffic benchmark is summarized using its final
30-minute Prometheus window, matching the steady-window approach used for the
Windy ingestion page. The presentation adds mean external-download and
successful-S3 payload throughput and image-size distribution alongside job,
latency, epoch, and EMA statistics.

The retained metrics contain exact S3 byte totals but no histogram restricted
to successful S3 PUT object sizes. The page therefore labels the p90
transformed-image size for images subsequently uploaded to S3; successful S3 and transformed-image
counts were nearly identical in the observed window. This limitation must not
be hidden or presented as an exact S3-only percentile.

### 2026-07-23 — Skaping freshness uses final-object ETag

**Affected component:** Skaping ingestion (checkpoint 11).

The initial design preferred the documented `media/getArchives` endpoint while
leaving resolved `latest_media` as an experiment. A bounded comparison found
that archives return the complete 24-hour image/video history, can be tens of
kilobytes per camera, and do not directly identify a point of view. By
contrast, all 41 active image points of view returned a body-free HTTP 302
whose final object exposed an ETag and `Last-Modified`; mean HEAD resolution
time was 0.126 seconds.

Checkpoint 11 therefore follows the stored `latest_media/mini` pointer and
uses the final object's ETag as the opaque marker. A changed image is accepted
only when its GET response repeats that ETag. `Last-Modified` is retained as
the provider-availability timestamp for latency observability, without claiming
that it is physical capture time. This avoids depending on the undocumented
timestamp components of the final URL and keeps freshness bandwidth below an
archive response. The trade-off is that one of ten compared cameras had an
archive entry five minutes newer than its latest-media pointer.

### 2026-07-23 — Skaping adaptive polling uses download time

**Affected component:** shared due selection and Skaping ingestion.

The initial adaptive formula uses a timestamp-shaped provider marker as its
second scheduling anchor. Skaping deliberately stores an opaque ETag instead,
and adding a separate persistent provider-timestamp column is deferred while
the small network is evaluated.

The shared query now accepts an explicit adaptive anchor. Its default remains
`provider_marker`, preserving Windy and Fintraffic behavior byte-for-byte at
their call sites. Only Skaping selects `download_timestamp`, for which a stream
is due when both conditions hold:

    now >= last_freshness_query_timestamp + minimum_ingestion_interval

    now >= last_download_timestamp
           + max(minimum_ingestion_interval,
                 polling_interval_factor * estimated_source_stream_period)

A missing freshness or download timestamp makes only its corresponding guard
immediately eligible. Consequently, a never-successful stream may be tried
initially, but any failed freshness/download attempt delays its next selection
by the minimum ingestion interval. This prevents a failing Skaping stream from
being queried in every short epoch. No schema migration is introduced.

The architecture document should distinguish the provider-marker and
download-timestamp anchor strategies, state that the former remains the
Windy/Fintraffic default, and record this Skaping policy as an explicit pilot
experiment rather than a provider-timestamp claim.

### 2026-07-24 — Ingestion presentation terminology

**Affected component:** static ingestion benchmark pages.

The externally presented outcome previously called `provider error` is now
called `freshness query error` for Windy, Fintraffic, and Skaping. This is a
presentation label for the existing `provider_error` metric outcome; metric
names and database behavior are unchanged. The term more directly identifies
the stage that failed without implying that every such outcome is a confirmed
provider-side fault.

### 2026-07-24 — Windy benchmark pacing experiment

**Affected component:** detached Windy benchmark recipe.

The benchmark-only `WINDY_INGESTION_REQUEST_DELAY_S` override is reduced from
0.01 to 0.001 seconds for the next 40-minute full-network experiment. This
raises the request-start pacing ceiling from approximately 100 to 1,000
requests per second while retaining 100 worker threads. Production defaults
are unchanged. The experiment must be evaluated through throttling, controlled
errors, epoch duration, provider latency, database-pool waiting, and payload
throughput before any operational default is changed.

### 2026-07-24 — Exact-age spool cleanup

**Affected component:** S3 image retention and checkpoint 12.

The initial design describes a daily deletion of complete date prefixes older
than the previous UTC day, giving an effective retention between roughly 24
and 48 hours. The implemented cleanup accepts an exact positive age in hours
and compares it with the download timestamp encoded in each canonical image
key. This permits the one-hour accelerated checkpoint validation while a
24-hour production invocation remains available.

Cleanup only recognizes the configured S3 prefix followed by the canonical
transformation/network/date/image layout. Unknown objects, malformed keys, and
the database-backup namespace are skipped. Dry-run and bounded deletion are
implementation safety controls not stated in the initial design.

### 2026-07-24 — PostgreSQL backups use the image-spool S3 tenancy

**Affected component:** database backup.

The initial design requires timestamped full PostgreSQL dumps in configured
backup storage but does not select a concrete storage backend or key layout.
The pilot stores custom-format dumps in the configured EWC S3 bucket under:

    backups/postgresql/YYYY/MM/DD/webcam_ingestion_YYYYMMDDTHHMMSSZ.dump

The namespace cannot match a canonical image key and is therefore excluded
from spool cleanup. Each upload records SHA-256 metadata and is verified by
content length and checksum metadata with a subsequent HEAD request.

For local validation, `pg_dump` runs inside the PostgreSQL 16 container so its
major version matches the server. The dump bytes are then uploaded by the
host-side Python command. Restoration is not automated.

### 2026-07-24 — Checkpoint-12 orchestration is deliberately bounded

**Affected component:** checkpoint sequencing and systemd.

Checkpoint 12 validates non-overlapping systemd scheduling with a ten-minute
cycle rather than installing the initial daily production policy. Discovery
is dry-run and sequential in Windy, Fintraffic, Skaping order; Windy is
restricted to Denmark. Ingestion remains continuous but each provider worker
selects at most five jobs per epoch. Real cleanup uses a one-hour cutoff and a
full database backup is created after every discovery sequence.

These accelerated units are kept in a validation-only directory. Full
provider scope, daily production schedules, secret-file deployment, and
operational recovery drills move to checkpoint 13.

### 2026-07-24 — Windy benchmark pacing restored after experiment

**Affected component:** detached Windy benchmark recipe.

The 0.001-second request-start pacing experiment produced no explicit HTTP 429
throttling and shortened mean epoch duration by about 10 percent relative to
the comparable 0.01-second run. Median provider-marker-to-download latency
improved, but p95 latency changed little, while controlled freshness-query
errors increased from roughly 20 to 127 per five minutes.

The benchmark override is therefore restored to 0.01 seconds. The normal
environment/production default remains unchanged at 0.1 seconds.

### 2026-07-24 — Distributed Windy scaling deferred to checkpoint 14

**Affected component:** deployment scaling and checkpoint sequencing.

The single-VM benchmarks do not establish whether the remaining Windy
constraint is per token, per source IP, per VM, or provider-wide. A separate
checkpoint 14 will compare the single-VM baseline with at least two VMs using
distinct authorized tokens and source IP addresses.

Windy cameras must be partitioned deterministically and exclusively: one
active owner per stream, with no implicit duplicate processing. PostgreSQL
remains the shared authoritative state store. The experiment begins with the
validated 0.01-second request pacing on each VM and compares throughput,
freshness failures, throttling, epoch duration, database contention, and
provider-marker latency. This work is deliberately excluded from checkpoint
13, which remains focused on single-VM production orchestration and recovery.

### 2026-07-24 — Four-day full-scope checkpoint-13 observation

**Affected component:** checkpoint-13 deployment validation.

Before failure-injection and recovery drills, checkpoint 13 uses a dedicated
four-day live-test target. It starts one full-scope worker for each provider,
uses deterministic ten-minute initial staggering, and schedules sequential
live discovery daily at 12:00 UTC. Cleanup uses the production 24-hour
retention threshold and runs at 00:15 UTC; database backup runs at 01:00 UTC.
An independent timer stops the test workers and schedules after four days but
leaves monitoring infrastructure available for final inspection.

The observation fixes provider freshness and download retry counts at zero,
retains the validated 0.01-second Windy request pacing and 0.1-second pacing
for Fintraffic and Skaping, and retains configured S3/MQTT retry behaviour.
Systemd still uses `Restart=on-failure` for unattended continuity, but no
failure is deliberately injected during this observation. Restart, recovery,
outbox, reboot, and restore drills remain separate checkpoint-13 acceptance
work.

The first launch used database-pool ceilings of 64, 64, and 16 for Windy,
Fintraffic, and Skaping. Their combined capacity exhausted PostgreSQL
connections and caused repeated ingestion epoch `OperationalError` failures.
The run was stopped and the ceilings were first corrected to 64, 24, and 8,
then reduced to 60, 20, and 8 to retain twelve PostgreSQL connection slots for
monitoring, discovery, maintenance, and administrative access.

### 2026-07-31 — Five-field ingestion state and epoch-end batching

**Affected component:** shared ingestion state and freshness comparison.

Ingestion state is limited to the last provider timestamp and marker associated
with a download decision, the local download-decision timestamp, the local
timestamp used in the last successfully published image name, and EMA.
Provider observations excluded by scheduling are discarded. Threads return
state transitions and the coordinator writes them as one epoch-end batch.

Windy supplies timestamp only. Fintraffic supplies its timestamp in the bulk
snapshot and ETag during JPEG GET. Skaping supplies `Last-Modified` and ETag
during HEAD. A download decision advances observed state and its valid EMA
candidate even if the body is later discarded or the pipeline fails.
`last_processed_timestamp` advances only after transformation, S3 upload, and
MQTT publication complete, preserving retry eligibility.

### 2026-07-30 — Durable publication outbox removed

**Affected component:** transformation, S3, MQTT, database schema, and worker
recovery.

The publication outbox introduced during checkpoint 7 is removed. Each source
job now performs transformation, S3 upload, and MQTT QoS 1 publication
synchronously. A failed pipeline leaves successfully processed freshness state
unchanged and is retried when the stream becomes due. Ambiguous failures may
therefore produce duplicate S3 PUTs or MQTT notifications, which the pilot
explicitly accepts.

This removes local image-payload accumulation and replay configuration. The
trade-off is the loss of durable mid-pipeline replay after process termination.

### 2026-07-30 — Direct S3 PUT and definitive delivery counters

**Affected component:** S3 storage and ingestion observability.

Derived-image upload no longer performs a preliminary S3 HEAD. Deterministic
keys may be overwritten by an ambiguous retry. SHA-256 object metadata remains
attached to each direct PUT.

`webcam_s3_upload_total` and `webcam_mqtt_publication_total` count completed
high-level operations exactly once as success or failure. Individual retry
attempts remain separate in `webcam_image_retry_total`.

### 2026-07-30 — Defensive database and epoch isolation

**Affected component:** worker database pool and concurrent result collection.

Every pooled Psycopg connection must return in IDLE state. Open or failed
transactions are rolled back; connections that cannot be restored are closed
and discarded. Unexpected per-job exceptions are logged with their source
stream, counted as `internal_error`, and do not prevent collection of the
remaining epoch futures.

### 2026-07-30 — Cleanup scope and soft PostgreSQL backup retention

**Affected component:** S3 maintenance.

Image cleanup is restricted to one explicit transformation prefix by default.
An operator must request `--all-transformation-prefixes` to cover every
recognized transformation layout.

After a new PostgreSQL dump has been uploaded and verified, backup cleanup
removes only the latest preceding available daily dump when it belongs to the
same calendar month. The final dump of each preceding month is retained.
Malformed and unexpected backup keys are never deleted automatically.

### 2026-07-30 — Application workflows move into Docker Compose

**Affected component:** pilot deployment before checkpoint 13 drills.

Windy, Fintraffic, and Skaping workers are long-lived Compose services built
from one application image. Discovery, image cleanup, and database backup are
short-lived runs of that same image. Compose owns application and
infrastructure restart policies, networking, secrets, health checks, and
persistent volumes.

Systemd is limited to VM-boot integration and daily one-shot scheduling.
Discovery runs sequentially at 12:00 UTC; verified backup and cleanup
maintenance run in sequence at 01:00 UTC. Checkpoint 13 operational drills
must validate this containerized model rather than the earlier host-Python
validation units.

### 2026-07-31 — Windy polling parameters reduced for latency experiment

**Affected component:** Windy ingestion scheduling only.

A read-only 1,000-stream visibility probe found that changed Windy
`lastUpdatedOn` values became visible after approximately 40 seconds at p50
and 70 seconds at p95, while the registry's latest provider-to-download gap
was 2.60 minutes at p50. To test EMA-driven polling as the remaining cause,
The initial latency experiment used a 120-second minimum publication interval,
a 120-second initial EMA, and a polling factor of 0.5. It did not improve the
full-window latency. Windy has therefore returned to a 300-second minimum and
a polling factor of 0.7. The current bounded-minimum experiment initializes the
period estimate as `NULL` and learns it from differences between successive
provider timestamps; Fintraffic and Skaping retain their existing EMA behavior.

### 2026-08-01 — Windy polling interval has a nine-minute floor

**Affected component:** Windy ingestion scheduling only.

A two-hour full-scale control without adaptive polling increased selected jobs
and batched provider requests by about 11.5 percent without increasing image
publication throughput or improving latency. Adaptive polling is retained with
lambda 0.7. Its provider-time guard is now
`max(540 seconds, 0.7 * estimated source-stream period)`, in addition to the
separate 300-second successful-publication guard. The 540-second floor affects
selection only and does not clamp the stored estimate. Fintraffic and Skaping
are unchanged.

### 2026-08-02 — Checkpoint-13 supervision and maintenance sequence

**Affected component:** production orchestration and recovery.

The initial architecture assigned application-worker restart and journal
ownership to templated systemd services. The retained implementation instead
uses independent long-lived Docker Compose services with
`restart: unless-stopped`; systemd is limited to VM-boot integration and the
scheduled maintenance trigger. This keeps the application runtime consistent
with the containerized implementation inherited and validated before
checkpoint 13.

The initial architecture also described discovery, image cleanup, and database
backup as separately scheduled jobs. Checkpoint 13 deliberately uses one
non-overlapping best-effort sequence. It runs image cleanup first, then the
three discoveries, then PostgreSQL backup and backup-retention cleanup. Each
operation runs in its own short-lived `docker compose run --rm` container with
a timeout. Failure or timeout is recorded but does not prevent the remaining
operations from running; aggregate failure is reported only after all steps
have been attempted. Cleanup is prioritized because missed retention can
exhaust storage.

Database restoration remains an explicit operator-approved maintenance
operation and never an automatic restart response. A restored dump retains
`estimated_source_stream_period` and the other ingestion-state fields. The
earlier idea of clearing legacy EMA-related state after restoration is dropped:
the bounded-minimum period estimator does not require that reset.

The implemented restore command additionally requires the selected canonical
S3 key to be repeated as an exact confirmation, verifies stored length and
SHA-256 metadata, validates the custom archive, and checks the restored
registry. A separate validation command performs the complete restore in a
disposable database without replacing live data. Workers remain stopped after
any failed live restore and restart only after successful validation.

Worker readiness is network-specific rather than tied to the short failure
backoff. The Windy readiness window is ten minutes and the Fintraffic and
Skaping windows are five minutes by default. This prevents legitimate long
epochs from being interpreted as failed worker health while retaining an
observable bound for stalled workers.

The checkpoint-13 unquiet drill uses the same containerized production path.
It injects a Windy container kill, a PostgreSQL restart that reuses the
persistent volume, and cleanup-first maintenance while all networks are
active. No destructive database restoration is part of that automated drill.
The worker fault is injected by signalling PID 1 inside the container; success
requires Docker's restart count to increase and the replacement process to
become healthy. Maintenance locking uses a UID-specific user-writable lock;
lock creation failure is distinct from a legitimate overlapping run.
