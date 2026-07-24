# webcam-iot-ingest

FastAPI service that ingests webcam images, stores them in S3-compatible object storage, and publishes notifications to an MQTT broker.

## Project slides

A presentation describing the repository and the current Windy discovery
statistics is available on [GitHub Pages](https://nanopiero.github.io/webcam-iot-ingest/).
The slide set also includes dedicated pages for
[Fintraffic discovery](https://nanopiero.github.io/webcam-iot-ingest/fintraffic.html),
[Skaping discovery](https://nanopiero.github.io/webcam-iot-ingest/skaping.html),
[Windy ingestion](https://nanopiero.github.io/webcam-iot-ingest/ingestion.html),
[Fintraffic ingestion](https://nanopiero.github.io/webcam-iot-ingest/fintraffic-ingestion.html),
and [Skaping ingestion](https://nanopiero.github.io/webcam-iot-ingest/skaping-ingestion.html).

Missing site altitudes may be enriched with the
[Open-Meteo Elevation API](https://open-meteo.com/en/docs/elevation-api),
using elevation data from the Copernicus DEM GLO-90 dataset. Open-Meteo and
Copernicus attribution applies to those derived values.

## What it does

1. Accepts a `POST /upload` payload with a base64-encoded image
2. Validates and converts the image to JPEG, resizes to max 640×480
3. Uploads the file to an S3 bucket
4. Publishes an MQTT message with the object URL and metadata

## Quick start

Install the repository-pinned `just` version without root privileges:

```bash
./install-just.sh
```

Adjust env variables and run
```bash
cp .env.example .env
just up
```

API docs available at http://localhost:8009/docs

## Local pilot infrastructure

PostgreSQL and Mosquitto run locally through Docker Compose. Their host
ports bind to loopback only. Create local configuration and a database
password before starting them:

```bash
cp .env.example .env
install -m 700 -d .secrets
touch .secrets/database_password
chmod 600 .secrets/database_password
# Open .secrets/database_password in an editor and enter the local password.
just infrastructure
uv run python -m database.healthcheck
```

`just infrastructure` runs
`docker compose --env-file .env up -d postgres mqtt`. It starts, or keeps
running, only the local PostgreSQL database and Mosquitto MQTT broker. It does
not start Prometheus, Grafana, discovery, or ingestion workers, and normal
invocations preserve the existing PostgreSQL volume and its data.

The `.env` and `.secrets/` paths are ignored by Git. Do not put provider,
database, MQTT, or S3 credentials in committed configuration. The local
Mosquitto listener permits anonymous clients and must not be exposed beyond
the VM loopback interface.

The PostgreSQL container initializes the pilot `network`, `site`, and
`source_stream` tables from `database/schema/001_pilot_schema.sql` on a new
data volume. Normal container restarts preserve the volume and its schema.
Avoid `just destroy` unless deleting all local service data is intentional.

## Spool cleanup and database backup

Inspect canonical derived-image objects older than an exact number of hours:

```bash
just cleanup-spool 24 --dry-run
```

Remove the qualifying image objects:

```bash
just cleanup-spool 24
```

Only keys matching the configured prefix and canonical
`T0V0/{network}/{YYYY}/{MM}/{DD}/{HH}/...jpg` layout are eligible. Unknown
keys and database backups are never deleted. Use `--limit N` for a bounded
validation deletion, and combine it with `--show-keys` during dry-run to
inspect the exact bounded scope.

Create a full PostgreSQL custom-format dump and upload it to the configured S3
bucket:

```bash
just backup-database --dry-run
just backup-database
```

Backups use timestamped keys below
`backups/postgresql/YYYY/MM/DD/`. The command verifies the stored length and
SHA-256 metadata after upload. It does not implement restoration.

## Webcam discovery

Run a complete provider discovery pass with:

```bash
just discover-fintraffic
just discover-skaping
just discover-windy
```

Both recipes forward workflow options. Use `--dry-run` to retrieve, validate,
and compare a complete snapshot without changing the registry:

```bash
just discover-fintraffic --dry-run
just discover-skaping --dry-run
just discover-windy --dry-run
```

Fintraffic uses the non-secret `FINTRAFFIC_USER_HEADER` application identifier
as the official `Digitraffic-User` request header. Set it to a meaningful
application name; the repository name is the default. Windy discovery requires
the API-key file and geographic configuration documented in `.env.example`.
Skaping discovery reads its API key from the ignored
`.secrets/skaping_api_key` file and selects only `image` points of view with
the `mini` rendition; it never prints the key or complete provider payload.
Fintraffic discovery retrieves the complete station list and then paced details
for every eligible station so descriptive station and preset metadata are
retained in the registry.

Run a bounded Fintraffic ingestion check without S3 or MQTT writes:

```bash
just ingest-fintraffic --limit 10 --dry-run
```

Run the equivalent ETag-based Skaping check, or explicitly exercise the full
T0V0/S3/MQTT path:

```bash
just ingest-skaping --limit 10 --dry-run
just ingest-skaping --limit 4 --publish
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `mqtt` | Broker hostname |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USERNAME` | — | Broker username |
| `MQTT_PASSWORD` | — | Broker password |
| `MQTT_TLS` | `False` | Enable TLS |
| `MQTT_TOPIC_PREPEND` | - | Topic prefix |
| `DATABASE_HOST` | `localhost` | PostgreSQL hostname |
| `DATABASE_PORT` | `5432` | PostgreSQL port |
| `DATABASE_NAME` | `webcam_ingestion` | PostgreSQL database |
| `DATABASE_USER` | `webcam_ingestion` | PostgreSQL user |
| `DATABASE_PASSWORD_FILE` | `.secrets/database_password` | Path to the database password file |
| `FINTRAFFIC_USER_HEADER` | `webcam-iot-ingest` | Non-secret application identifier sent as `Digitraffic-User` |
| `FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Fintraffic bulk freshness request |
| `FINTRAFFIC_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Fintraffic JPEG request |
| `SKAPING_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Skaping HEAD/ETag request |
| `SKAPING_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Skaping mini-image request |
| `WINDY_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Windy metadata/freshness HTTP request |
| `WINDY_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Windy image-download HTTP request |
| `BUCKET_NAME` | - | Bucket name |
| `BUCKET_ACCESS_KEY_ID` | — | S3 access key |
| `BUCKET_SECRET_ACCESS_KEY` | — | S3 secret key |
| `BUCKET_ENDPOINT_URL` | — | S3 endpoint URL |
| `BUCKET_OBJECT_URL` | — | S3 object URL |
| `BATCH_METRICS_ENABLED` | `true` | Publish cleanup and backup metrics through Pushgateway |
| `BATCH_METRICS_GATEWAY_URL` | `http://localhost:9091` | Operational batch Pushgateway |
| `DATABASE_BACKUP_S3_PREFIX` | `backups/postgresql` | S3 namespace for timestamped database dumps |
| `PG_DUMP_MODE` | `docker-compose` in `.env.example` | Run the server-matched `pg_dump` inside the PostgreSQL container |

## Monitoring

Start the full monitoring stack (Prometheus + Grafana):

```bash
just local
```

- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

Run a detached, monitored Windy benchmark with
[just](https://github.com/casey/just):

```bash
just ingestion-test all_windy 20m
```

`all_windy` selects every configured EUMETNET country and `20m` runs for 20
minutes. A country subset can be supplied as `FR,DE`; durations accept `s`,
`m`, or `h`. Once the deadline is reached, no new epoch starts, but an active
epoch finishes normally. Every invocation creates a uniquely hashed `screen`
session and a timestamped `/tmp` log; the recipe prints both names when it
starts.

Add the optional `staggered` mode to spread the first freshness checks over a
configurable window instead of creating a synchronized cold-start peak:

```bash
just ingestion-test FR,DE 20m staggered
```

Fintraffic has a separate worker, metrics endpoint, and Grafana dashboard, so
it can run concurrently with Windy. For example:

```bash
just ingestion-test-fintraffic 2h staggered
```

The Fintraffic staggered mode uses a fixed seed and a 600-second initial
window. Each epoch uses one bulk preset `measuredTime` snapshot for freshness,
then downloads only changed full-JPEG images.

Skaping has its own worker on metrics port 8015 and can run beside both other
providers:

```bash
just ingestion-test-skaping 2h staggered
```

It obtains the final mini object's ETag with a followed HEAD request, downloads
only changed objects, and validates that the GET response has the same ETag.
The final object's HTTP `Last-Modified` value is retained as the
provider-availability timestamp; it is not claimed to be the physical capture
time. For the pilot, Skaping alone anchors adaptive polling to the last
successful download and also requires 300 seconds since the last freshness
attempt. Windy and Fintraffic retain the shared provider-marker timestamp
strategy. The deterministic stagger window is 600 seconds.

All provider ingestion dashboards include source and derived image size, width, height,
color depth, format, and color-mode observability. Size, width, height, and
color-depth panels show rolling P50, P90, and P95 values over five minutes.
They also show one-minute-smoothed external image-download and successful S3
PUT payload throughput. S3 throughput excludes objects reused after immutable
identity verification because no object bytes are retransmitted.

This benchmark-only mode uses the fixed seed `windy-benchmark-v1` and a
deterministic hash of each source-stream ID to assign a phase in `[0, 600)`
seconds by default. `INITIAL_STAGGER_WINDOW_S` configures this window, and the
benchmark recipe sets it explicitly to 600 seconds. The initial phase is
independent of EMA. The mode changes neither
registry timestamps nor EMA values; normal adaptive scheduling takes over after
a stream's first check. As with a direct run's first epoch, EMA candidates from
each stream's staggered first check are discarded to avoid cold-start bias. The
worker prints the seed and phase window in its log.

The benchmark keeps two independent timing controls explicit:

- `MINIMUM_INGESTION_INTERVAL_S=300` is the per-webcam download-time guard;
- after a download, normal selection also waits until the stored provider
  timestamp plus `POLLING_INTERVAL_FACTOR * ema_download_period`;
- `INGESTION_MIN_EPOCH_PERIOD_S=15` prevents excessively rapid epochs;
- `INGESTION_IDLE_DELAY_S=0` adds no post-epoch pause; the 15-second minimum
  epoch period still prevents a tight loop for short or empty epochs.

### Reproducible monitoring versions

| Component | Version | Pin location |
|---|---:|---|
| Prometheus | 3.13.1 | `docker-compose.yml` image tag |
| Prometheus Pushgateway | 1.11.3 | `docker-compose.yml` image tag |
| Grafana OSS | 11.2.0 | `docker-compose.yml` image tag |
| postgres_exporter | 0.19.1 | `docker-compose.yml` image tag |
| node_exporter | 1.11.1 | `docker-compose.yml` image tag |
| cAdvisor | 0.57.0 | `docker-compose.yml` image tag |
| just | 1.57.0 | `justfile` and `install-just.sh` |

The Compose image tags make monitoring recreation deterministic at the release
version level. For byte-identical container images, deployments may additionally
lock the resolved image digests in their deployment manifest.

Pre-built dashboards for FastAPI, MQTT, all three ingestion workers, and all
three discovery providers are provisioned automatically. Short-lived discovery jobs publish
their persistent batch metrics to the locally bound Pushgateway on port 9091;
Prometheus scrapes it and Grafana displays the retained results after the
discovery process exits.

The monitoring profile also starts PostgreSQL, host, and container exporters.
Grafana provisions an **Infrastructure health** dashboard and an
**Operational batch jobs** dashboard. Alert rules cover unavailable
infrastructure targets, unavailable checkpoint workers, low host disk space,
PostgreSQL failure, and cleanup or backup failures.

## Checkpoint-12 systemd validation

The units in `deployment/systemd/checkpoint12-validation/` are deliberately
accelerated validation material, not production policy. They run:

- three continuous ingestion workers, bounded to five selected jobs per epoch;
- Windy ingestion and discovery restricted to Denmark;
- sequential dry-run discovery for Windy, Fintraffic, and Skaping;
- real cleanup of canonical image objects older than one hour;
- a real full PostgreSQL backup to S3;
- a maintenance cycle every ten minutes, measured from completion so cycles
  cannot overlap.

Install the units only on the current pilot VM after reviewing their absolute
working-directory and executable paths:

```bash
sudo cp deployment/systemd/checkpoint12-validation/webcam-checkpoint12-* \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start webcam-checkpoint12-infrastructure.service
sudo systemctl start webcam-checkpoint12-ingestion@windy.service
sudo systemctl start webcam-checkpoint12-ingestion@fintraffic.service
sudo systemctl start webcam-checkpoint12-ingestion@skaping.service
sudo systemctl enable --now webcam-checkpoint12-cycle.timer
```

Inspect the accelerated cycle with:

```bash
systemctl list-timers webcam-checkpoint12-cycle.timer
systemctl status webcam-checkpoint12-cycle.service
journalctl -u webcam-checkpoint12-cycle.service
```

Full-scale production scheduling and operational recovery drills belong to
checkpoint 13.

## Running tests

```bash
pip install -e .
pytest tests/
```

## Example image ingestion
Example Python script to ingest a randomly colored 640x480 png image provided in `test_sender.py`
