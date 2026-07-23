# webcam-iot-ingest

FastAPI service that ingests webcam images, stores them in S3-compatible object storage, and publishes notifications to an MQTT broker.

## Project slides

A presentation describing the repository and the current Windy discovery
statistics is available on [GitHub Pages](https://nanopiero.github.io/webcam-iot-ingest/).

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
docker compose --env-file .env up -d postgres mqtt
uv run python -m database.healthcheck
```

The `.env` and `.secrets/` paths are ignored by Git. Do not put provider,
database, MQTT, or S3 credentials in committed configuration. The local
Mosquitto listener permits anonymous clients and must not be exposed beyond
the VM loopback interface.

The PostgreSQL container initializes the pilot `network`, `site`, and
`source_stream` tables from `database/schema/001_pilot_schema.sql` on a new
data volume. Normal container restarts preserve the volume and its schema.
Avoid `just destroy` unless deleting all local service data is intentional.

## Webcam discovery

Run a complete provider discovery pass with:

```bash
just discover-fintraffic
just discover-windy
```

Both recipes forward workflow options. Use `--dry-run` to retrieve, validate,
and compare a complete snapshot without changing the registry:

```bash
just discover-fintraffic --dry-run
just discover-windy --dry-run
```

Fintraffic uses the non-secret `FINTRAFFIC_USER_HEADER` application identifier
as the official `Digitraffic-User` request header. Set it to a meaningful
application name; the repository name is the default. Windy discovery requires
the API-key file and geographic configuration documented in `.env.example`.

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
| `WINDY_FRESHNESS_QUERY_RETRY_COUNT` | `1` | Retries after the initial Windy metadata/freshness HTTP request |
| `WINDY_DOWNLOAD_RETRY_COUNT` | `1` | Retries after the initial Windy image-download HTTP request |
| `BUCKET_NAME` | - | Bucket name |
| `BUCKET_ACCESS_KEY_ID` | — | S3 access key |
| `BUCKET_SECRET_ACCESS_KEY` | — | S3 secret key |
| `BUCKET_ENDPOINT_URL` | — | S3 endpoint URL |
| `BUCKET_OBJECT_URL` | — | S3 object URL |

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
| Grafana OSS | 11.2.0 | `docker-compose.yml` image tag |
| just | 1.57.0 | `justfile` and `install-just.sh` |

The Compose image tags make monitoring recreation deterministic at the release
version level. For byte-identical container images, deployments may additionally
lock the resolved image digests in their deployment manifest.

Pre-built dashboards for FastAPI and MQTT are provisioned automatically.

## Running tests

```bash
pip install -e .
pytest tests/
```

## Example image ingestion
Example Python script to ingest a randomly colored 640x480 png image provided in `test_sender.py`
