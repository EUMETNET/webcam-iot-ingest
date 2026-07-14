# webcam-iot-ingest

FastAPI service that ingests webcam images, stores them in S3-compatible object storage, and publishes notifications to an MQTT broker.

## What it does

1. Accepts a `POST /upload` payload with a base64-encoded image
2. Validates and converts the image to JPEG, resizes to max 640×480
3. Uploads the file to an S3 bucket
4. Publishes an MQTT message with the object URL and metadata

## Quick start

Install just
```bash
install-just.sh
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

Pre-built dashboards for FastAPI and MQTT are provisioned automatically.

## Running tests

```bash
pip install -e .
pytest tests/
```

## Example image ingestion
Example Python script to ingest a randomly colored 640x480 png image provided in `test_sender.py`
