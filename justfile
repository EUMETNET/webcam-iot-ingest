# list recipes
default:
    @just --list

set positional-arguments

just_version := "1.57.0"

# Build and run the default docker services
up: build services

# Build and run the default docker services and start up monitoring
local: up monitoring

# ---------------------------------------------------------------------------- #
#                                    build                                     #
# ---------------------------------------------------------------------------- #

# Build the docker images
build:
    docker compose --env-file .env --profile monitoring build --no-cache

# # ---------------------------------------------------------------------------- #
# #                                     local                                    #
# # ---------------------------------------------------------------------------- #

# Start the default docker compose containers
services:
    docker compose --env-file .env up -d

# Start only the local PostgreSQL and MQTT infrastructure
infrastructure:
    docker compose --env-file .env up -d postgres mqtt

# Check that PostgreSQL is reachable and the pilot schema exists
database-check:
    docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.healthcheck

# Run one complete Fintraffic discovery pass. Add --dry-run to avoid writes.
discover-fintraffic *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec just container-discover fintraffic "$@"

# Run one complete Windy discovery pass. Add --dry-run to avoid writes.
discover-windy *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec just container-discover windy "$@"

# Run one complete Skaping discovery pass. Add --dry-run to avoid writes.
discover-skaping *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec just container-discover skaping "$@"

# Inspect or delete canonical derived-image objects older than an exact age.
# Example: just cleanup-spool 24 --dry-run
cleanup-spool older_than_hours *args:
    #!/usr/bin/env bash
    set -euo pipefail
    older_than_hours="$1"
    shift
    exec just container-cleanup-spool "$older_than_hours" "$@"

# Create a timestamped full PostgreSQL dump in the configured S3 bucket.
# Add --dry-run to validate configuration and inspect the future key.
backup-database *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec just container-backup-database "$@"

# Build and start the final containerized ingestion and monitoring stack.
container-stack-up:
    docker compose --env-file .env --profile application --profile monitoring up -d --build

# Stop the final containerized stack without removing persistent volumes.
container-stack-stop:
    docker compose --env-file .env --profile application --profile monitoring stop

# Run one provider discovery in the shared short-lived application container.
container-discover network *args:
    #!/usr/bin/env bash
    set -euo pipefail
    network="$1"
    shift
    exec deployment/systemd/pilot/run-discovery "$network" "$@"

# Run transformation-scoped image cleanup in a short-lived container.
container-cleanup-spool older_than_hours="24" *args:
    #!/usr/bin/env bash
    set -euo pipefail
    older_than_hours="$1"
    shift
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m storage.s3_spool_cleanup \
        --older-than-hours "$older_than_hours" "$@"

# Create and verify a database dump, then clean the prior same-month daily dump.
container-backup-database *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.database_backup \
        --pg-dump-mode direct "$@"

# Inspect or run conservative cleanup for a verified canonical backup key.
container-cleanup-database-backups current_key *args:
    #!/usr/bin/env bash
    set -euo pipefail
    current_key="$1"
    shift
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.database_backup_cleanup \
        --current-key "$current_key" "$@"

# One dry-run discovery used by the accelerated checkpoint-12 systemd chain.
checkpoint12-discover network:
    #!/usr/bin/env bash
    set -euo pipefail
    case "$1" in
        windy)
            exec env WINDY_MEMBER_COUNTRIES=DK \
                UV_CACHE_DIR=/tmp/webcam-uv-cache \
                uv run --env-file .env python -m \
                discovery.windy.windy_discovery_workflow --dry-run
            ;;
        fintraffic)
            exec env UV_CACHE_DIR=/tmp/webcam-uv-cache \
                uv run --env-file .env python -m \
                discovery.fintraffic.fintraffic_discovery_workflow --dry-run
            ;;
        skaping)
            exec env UV_CACHE_DIR=/tmp/webcam-uv-cache \
                uv run --env-file .env python -m \
                discovery.skaping.skaping_discovery_workflow --dry-run
            ;;
        *)
            echo "network must be windy, fintraffic, or skaping" >&2
            exit 2
            ;;
    esac

# Checkpoint-12 validation worker: deliberately bounded, not production policy.
checkpoint12-ingest network limit="5":
    #!/usr/bin/env bash
    set -euo pipefail
    network="$1"
    limit="$2"
    case "$network" in
        windy)
            module="ingestion.worker"
            network_args=(--network win --countries DK)
            port=8113
            ;;
        fintraffic)
            module="ingestion.fintraffic.worker"
            network_args=()
            port=8114
            ;;
        skaping)
            module="ingestion.skaping.worker"
            network_args=()
            port=8115
            ;;
        *)
            echo "network must be windy, fintraffic, or skaping" >&2
            exit 2
            ;;
    esac
    exec env \
        MQTT_HOST=127.0.0.1 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT="$port" \
        INGESTION_WORKER_THREADS="$limit" \
        INGESTION_DATABASE_POOL_SIZE="$limit" \
        INGESTION_MAX_JOBS_PER_EPOCH="$limit" \
        INGESTION_IDLE_DELAY_S=0 \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        uv run --env-file .env python -m "$module" \
        --max-jobs "$limit" --stagger-initial-polling "${network_args[@]}"

# Full-scope worker used by the checkpoint-13 four-day live test.
checkpoint13-ingest network:
    #!/usr/bin/env bash
    set -euo pipefail
    case "$1" in
        windy)
            module="ingestion.worker"
            max_jobs=30000
            threads=100
            pool_size=60
            port=8013
            network_args=(--network win)
            provider_env=(
                WINDY_INGESTION_REQUEST_DELAY_S=0.01
                WINDY_FRESHNESS_QUERY_RETRY_COUNT=0
                WINDY_DOWNLOAD_RETRY_COUNT=0
            )
            ;;
        fintraffic)
            module="ingestion.fintraffic.worker"
            max_jobs=3000
            threads=50
            pool_size=20
            port=8014
            network_args=()
            provider_env=(
                FINTRAFFIC_INGESTION_REQUEST_DELAY_S=0.1
                FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT=0
                FINTRAFFIC_DOWNLOAD_RETRY_COUNT=0
            )
            ;;
        skaping)
            module="ingestion.skaping.worker"
            max_jobs=100
            threads=16
            pool_size=8
            port=8015
            network_args=()
            provider_env=(
                SKAPING_INGESTION_REQUEST_DELAY_S=0.1
                SKAPING_FRESHNESS_QUERY_RETRY_COUNT=0
                SKAPING_DOWNLOAD_RETRY_COUNT=0
            )
            ;;
        *)
            echo "network must be windy, fintraffic, or skaping" >&2
            exit 2
            ;;
    esac
    exec env \
        MQTT_HOST=127.0.0.1 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT="$port" \
        INGESTION_WORKER_THREADS="$threads" \
        INGESTION_DATABASE_POOL_SIZE="$pool_size" \
        INGESTION_MAX_JOBS_PER_EPOCH="$max_jobs" \
        INGESTION_IDLE_DELAY_S=0 \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        "${provider_env[@]}" \
        uv run --env-file .env python -m "$module" \
        --max-jobs "$max_jobs" --stagger-initial-polling "${network_args[@]}"

# Run a bounded Windy ingestion sample. Add --dry-run to avoid S3/MQTT.
ingest-windy *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m \
        ingestion.windy.windy_ingestion_workflow "$@"

# Run a bounded Fintraffic ingestion sample. Add --dry-run to avoid S3/MQTT.
ingest-fintraffic *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m \
        ingestion.fintraffic.fintraffic_ingestion_workflow "$@"

# Run a bounded Skaping ingestion sample. Add --dry-run to avoid S3/MQTT.
ingest-skaping *args:
    #!/usr/bin/env bash
    set -euo pipefail
    exec docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m \
        ingestion.skaping.skaping_ingestion_workflow "$@"

# Start the monitoring containers
monitoring:
    docker compose --env-file .env --profile monitoring up -d

# Run a monitored Windy ingestion benchmark in a detached screen session.
# Scope is "all_windy" for every EUMETNET country, or a comma-separated list such as FR,DE.
ingestion-test scope duration mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    scope="$1"
    duration="$2"
    mode="$3"
    if [[ "$(just --version)" != "just {{ just_version }}" ]]; then
        echo "this repository requires just {{ just_version }}" >&2
        exit 2
    fi
    if [[ "$scope" != "all_windy" && ! "$scope" =~ ^[A-Za-z]{2}(,[A-Za-z]{2})*$ ]]; then
        echo "scope must be 'all_windy' or comma-separated ISO alpha-2 codes (for example FR,DE)" >&2
        exit 2
    fi
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h (for example 20m)" >&2
        exit 2
    fi
    if [[ -n "$mode" && "$mode" != "staggered" && "$mode" != "batched" && "$mode" != "staggered-batched" ]]; then
        echo "optional mode must be 'staggered', 'batched', or 'staggered-batched'" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    safe_scope="${scope//,/-}"
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mode_name="${mode:-direct}"
    run_hash="$(printf '%s' "${timestamp}-${scope}-${duration}-${mode_name}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="windy-${safe_scope,,}-${duration}-${mode_name}-${run_hash}"
    log="/tmp/windy-${safe_scope,,}-${duration}-${mode_name}-${timestamp}-${run_hash}.log"
    docker compose --env-file .env --profile monitoring up -d \
        postgres mqtt prometheus grafana
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec just _ingestion-test-foreground '$scope' '$run_seconds' '$mode'"
    echo "started screen session: ${session}"
    echo "log: ${log}"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

_ingestion-test-foreground scope run_seconds mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    scope="$1"
    mode="$3"
    countries=()
    stagger=()
    batching=()
    if [[ "$scope" != "all_windy" ]]; then
        countries=(--countries "${scope^^}")
    fi
    if [[ "$mode" == "staggered" || "$mode" == "staggered-batched" ]]; then
        stagger=(--stagger-initial-polling)
    fi
    if [[ "$mode" == "batched" || "$mode" == "staggered-batched" ]]; then
        batching=(--batch-freshness)
    fi
    exec env \
        MQTT_HOST=127.0.0.1 \
        WINDY_INGESTION_REQUEST_DELAY_S=0.01 \
        INGESTION_WORKER_THREADS=100 \
        INGESTION_DATABASE_POOL_SIZE=64 \
        INGESTION_MAX_JOBS_PER_EPOCH=30000 \
        MINIMUM_INGESTION_INTERVAL_S=300 \
        INGESTION_MIN_EPOCH_PERIOD_S=15 \
        INGESTION_IDLE_DELAY_S=0 \
        INITIAL_STAGGER_WINDOW_S=600 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT=8013 \
        POLLING_INTERVAL_FACTOR=0.7 \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        uv run --env-file .env python -m ingestion.worker \
            --network win \
            --max-jobs 30000 \
            --run-for-seconds "$2" \
            --verbose \
            "${stagger[@]}" \
            "${batching[@]}" \
            "${countries[@]}"

# Run the Fintraffic worker for a duration in a detached, monitored screen.
# Optional "staggered" mode spreads initial checks over 10 minutes.
ingestion-test-fintraffic duration mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    mode="$2"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h" >&2
        exit 2
    fi
    if [[ -n "$mode" && "$mode" != "staggered" ]]; then
        echo "optional mode must be 'staggered'" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mode_name="${mode:-direct}"
    run_hash="$(printf '%s' "${timestamp}-${duration}-${mode_name}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="fintraffic-${duration}-${mode_name}-${run_hash}"
    log="/tmp/fintraffic-${duration}-${mode_name}-${timestamp}-${run_hash}.log"
    docker compose --env-file .env --profile monitoring up -d \
        postgres mqtt prometheus grafana
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec just _ingestion-test-fintraffic-foreground '$run_seconds' '$mode'"
    echo "started screen session: ${session}"
    echo "log: ${log}"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

_ingestion-test-fintraffic-foreground run_seconds mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    stagger=()
    if [[ "$2" == "staggered" ]]; then
        stagger=(--stagger-initial-polling)
    fi
    exec env \
        MQTT_HOST=127.0.0.1 \
        FINTRAFFIC_INGESTION_REQUEST_DELAY_S=0.1 \
        FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT=0 \
        FINTRAFFIC_DOWNLOAD_RETRY_COUNT=0 \
        INGESTION_WORKER_THREADS=100 \
        INGESTION_DATABASE_POOL_SIZE=64 \
        INGESTION_MAX_JOBS_PER_EPOCH=3000 \
        MINIMUM_INGESTION_INTERVAL_S=300 \
        INGESTION_MIN_EPOCH_PERIOD_S=15 \
        INGESTION_IDLE_DELAY_S=0 \
        INITIAL_STAGGER_WINDOW_S=600 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT=8014 \
        POLLING_INTERVAL_FACTOR=0.7 \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        uv run --env-file .env python -m ingestion.fintraffic.worker \
            --max-jobs 3000 \
            --run-for-seconds "$1" \
            --verbose \
            "${stagger[@]}"

# Run the monitored Skaping worker for a duration in a detached screen.
ingestion-test-skaping duration mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    mode="$2"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h" >&2
        exit 2
    fi
    if [[ -n "$mode" && "$mode" != "staggered" ]]; then
        echo "optional mode must be 'staggered'" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mode_name="${mode:-direct}"
    run_hash="$(printf '%s' "${timestamp}-${duration}-${mode_name}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="skaping-${duration}-${mode_name}-${run_hash}"
    log="/tmp/skaping-${duration}-${mode_name}-${timestamp}-${run_hash}.log"
    docker compose --env-file .env --profile monitoring up -d \
        postgres mqtt prometheus grafana
    docker compose --env-file .env --profile monitoring kill -s HUP prometheus
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec just _ingestion-test-skaping-foreground '$run_seconds' '$mode'"
    echo "started screen session: ${session}"
    echo "log: ${log}"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

_ingestion-test-skaping-foreground run_seconds mode="":
    #!/usr/bin/env bash
    set -euo pipefail
    stagger=()
    if [[ "$2" == "staggered" ]]; then
        stagger=(--stagger-initial-polling)
    fi
    exec env \
        MQTT_HOST=127.0.0.1 \
        SKAPING_INGESTION_REQUEST_DELAY_S=0.1 \
        SKAPING_FRESHNESS_QUERY_RETRY_COUNT=0 \
        SKAPING_DOWNLOAD_RETRY_COUNT=0 \
        INGESTION_WORKER_THREADS=16 \
        INGESTION_DATABASE_POOL_SIZE=16 \
        INGESTION_MAX_JOBS_PER_EPOCH=100 \
        MINIMUM_INGESTION_INTERVAL_S=300 \
        INGESTION_MIN_EPOCH_PERIOD_S=15 \
        INGESTION_IDLE_DELAY_S=0 \
        INITIAL_STAGGER_WINDOW_S=600 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT=8015 \
        POLLING_INTERVAL_FACTOR=0.7 \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        uv run --env-file .env python -m ingestion.skaping.worker \
            --max-jobs 100 \
            --run-for-seconds "$1" \
            --verbose \
            "${stagger[@]}"

# Stop all containers and remove their volumes (destructive: deletes local data)
destroy:
    docker compose --profile monitoring down --volumes
