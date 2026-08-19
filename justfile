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

# List canonical PostgreSQL dumps available in S3.
list-database-backups:
    docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.database_restore --list

# Download, checksum, and inspect a dump without changing PostgreSQL.
validate-database-backup object_key:
    docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.database_restore \
        --object-key "{{ object_key }}" --dry-run

# Restore into a disposable database, validate it, and remove it afterwards.
validate-database-restore object_key:
    #!/usr/bin/env bash
    set -euo pipefail
    object_key="$1"
    test_database="webcam_restore_validation_$(date -u +%Y%m%d%H%M%S)_${RANDOM}"
    cleanup() {
        docker compose --env-file .env exec -T postgres \
            dropdb --if-exists --force --username webcam_ingestion \
            "$test_database" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT
    docker compose --env-file .env exec -T postgres \
        createdb --username webcam_ingestion "$test_database"
    docker compose --env-file .env --profile jobs run --rm \
        -e BATCH_METRICS_ENABLED=false \
        webcam-job python -m database.database_restore \
        --object-key "$object_key" --confirm-object-key "$object_key" \
        --target-database "$test_database"
    echo "Isolated restore validated in $test_database; removing it now."

# Explicit destructive maintenance operation. Workers stay stopped on failure.
restore-database object_key:
    #!/usr/bin/env bash
    set -euo pipefail
    object_key="$1"
    echo "Stopping ingestion workers before restoring: $object_key"
    docker compose --env-file .env --profile application stop \
        windy-worker fintraffic-worker skaping-worker
    if ! docker compose --env-file .env --profile jobs run --rm \
        webcam-job python -m database.database_restore \
        --object-key "$object_key" --confirm-object-key "$object_key"; then
        echo "Restore failed; ingestion workers remain stopped." >&2
        exit 1
    fi
    docker compose --env-file .env run --rm schema-migrate
    docker compose --env-file .env --profile application start \
        windy-worker fintraffic-worker skaping-worker
    echo "Restore validated; ingestion workers restarted."

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

# Full-scale Windy test using a NULL-initialized bounded provider-period estimate.
windy-period-test duration="2h":
    just ingestion-test all_windy "{{ duration }}" staggered-batched-period-min

# Full-scale Windy control test without adaptive polling. The per-stream
# successful-publication guard remains active.
windy-no-adaptive-test duration="2h":
    just ingestion-test all_windy "{{ duration }}" staggered-batched-no-adaptive

# Full-scale Windy test with max(9 minutes, 0.7 * estimated period) polling.
windy-nine-minute-floor-test duration="2h":
    just ingestion-test all_windy "{{ duration }}" staggered-batched-nine-minute-floor

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
    if [[ -n "$mode" && "$mode" != "staggered" && "$mode" != "batched" && "$mode" != "staggered-batched" && "$mode" != "staggered-batched-period-min" && "$mode" != "staggered-batched-no-adaptive" && "$mode" != "staggered-batched-nine-minute-floor" ]]; then
        echo "unsupported Windy benchmark mode: $mode" >&2
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
    period_estimator=()
    polling_factor=0.7
    polling_floor=540
    if [[ "$scope" != "all_windy" ]]; then
        countries=(--countries "${scope^^}")
    fi
    if [[ "$mode" == "staggered" || "$mode" == "staggered-batched" || "$mode" == "staggered-batched-period-min" || "$mode" == "staggered-batched-no-adaptive" || "$mode" == "staggered-batched-nine-minute-floor" ]]; then
        stagger=(--stagger-initial-polling)
    fi
    if [[ "$mode" == "batched" || "$mode" == "staggered-batched" || "$mode" == "staggered-batched-period-min" || "$mode" == "staggered-batched-no-adaptive" || "$mode" == "staggered-batched-nine-minute-floor" ]]; then
        batching=(--batch-freshness)
    fi
    if [[ "$mode" == "staggered-batched-period-min" ]]; then
        period_estimator=(--reset-windy-period-estimates)
        polling_factor=0.7
    fi
    if [[ "$mode" == "staggered-batched-no-adaptive" ]]; then
        polling_factor=0
        polling_floor=0
    fi
    if [[ "$mode" == "staggered-batched-nine-minute-floor" ]]; then
    fi
    exec env \
        MQTT_HOST=127.0.0.1 \
        WINDY_INGESTION_REQUEST_DELAY_S=0.01 \
        INGESTION_WORKER_THREADS=100 \
        INGESTION_DATABASE_POOL_SIZE=64 \
        INGESTION_MAX_JOBS_PER_EPOCH=30000 \
        WINDY_MINIMUM_INGESTION_INTERVAL_S=300 \
        INGESTION_MIN_EPOCH_PERIOD_S=15 \
        INGESTION_IDLE_DELAY_S=0 \
        INITIAL_STAGGER_WINDOW_S=600 \
        INGESTION_HEALTH_HOST=0.0.0.0 \
        INGESTION_HEALTH_PORT=8013 \
        WINDY_POLLING_INTERVAL_FACTOR="$polling_factor" \
        WINDY_MINIMUM_POLLING_INTERVAL_S="$polling_floor" \
        UV_CACHE_DIR=/tmp/webcam-uv-cache \
        uv run --env-file .env python -m ingestion.worker \
            --network win \
            --max-jobs 30000 \
            --run-for-seconds "$2" \
            --verbose \
            "${stagger[@]}" \
            "${batching[@]}" \
            "${period_estimator[@]}" \
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

# Run concurrent, containerized Fintraffic and Skaping benchmarks. Each
# container is limited to half a CPU while retaining the
# intended I/O thread count. Containers remain available for log inspection.
ingestion-test-fintraffic-skaping duration="40m":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_hash="$(printf '%s' "${timestamp}-${duration}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    fin_name="webcam-fintraffic-${duration}-${run_hash}"
    ska_name="webcam-skaping-${duration}-${run_hash}"

    docker compose --env-file .env --profile monitoring up -d \
        postgres mqtt prometheus grafana
    docker compose --env-file .env build webcam-job

    docker compose --env-file .env run -d --no-deps \
        --name "$fin_name" \
        --publish 127.0.0.1:8014:8014 \
        -e INGESTION_HEALTH_HOST=0.0.0.0 \
        -e INGESTION_HEALTH_PORT=8014 \
        -e INGESTION_WORKER_THREADS=4 \
        -e INGESTION_DATABASE_POOL_SIZE=8 \
        -e INGESTION_MAX_JOBS_PER_EPOCH=3000 \
        -e MINIMUM_INGESTION_INTERVAL_S=300 \
        -e FINTRAFFIC_MINIMUM_POLLING_INTERVAL_S=480 \
        -e POLLING_INTERVAL_FACTOR=0.7 \
        -e INGESTION_MIN_EPOCH_PERIOD_S=15 \
        -e INGESTION_IDLE_DELAY_S=0 \
        -e INITIAL_STAGGER_WINDOW_S=600 \
        -e FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT=0 \
        -e FINTRAFFIC_DOWNLOAD_RETRY_COUNT=0 \
        webcam-job python -m ingestion.fintraffic.worker \
            --max-jobs 3000 --run-for-seconds "$run_seconds" \
            --stagger-initial-polling --verbose

    docker compose --env-file .env run -d --no-deps \
        --name "$ska_name" \
        --publish 127.0.0.1:8015:8015 \
        -e INGESTION_HEALTH_HOST=0.0.0.0 \
        -e INGESTION_HEALTH_PORT=8015 \
        -e INGESTION_WORKER_THREADS=2 \
        -e INGESTION_DATABASE_POOL_SIZE=4 \
        -e INGESTION_MAX_JOBS_PER_EPOCH=100 \
        -e MINIMUM_INGESTION_INTERVAL_S=300 \
        -e SKAPING_MINIMUM_POLLING_INTERVAL_S=240 \
        -e POLLING_INTERVAL_FACTOR=0.7 \
        -e INGESTION_MIN_EPOCH_PERIOD_S=15 \
        -e INGESTION_IDLE_DELAY_S=0 \
        -e INITIAL_STAGGER_WINDOW_S=600 \
        -e SKAPING_FRESHNESS_QUERY_RETRY_COUNT=0 \
        -e SKAPING_DOWNLOAD_RETRY_COUNT=0 \
        webcam-job python -m ingestion.skaping.worker \
            --max-jobs 100 --run-for-seconds "$run_seconds" \
            --stagger-initial-polling --verbose

    docker update --cpus 0.5 "$fin_name" "$ska_name" >/dev/null
    echo "Fintraffic container: $fin_name (0.5 CPU, 4 threads, 8-minute floor)"
    echo "Skaping container:    $ska_name (0.5 CPU, 2 threads, 4-minute floor)"
    echo "Follow logs: docker logs -f $fin_name"
    echo "Follow logs: docker logs -f $ska_name"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

# Run all three ingestion networks through the complete publication pipeline.
# The worker and maintenance quotas total 7.5 CPUs, leaving 0.5 CPU on an
# eight-core VM for PostgreSQL, MQTT, and monitoring.
ingestion-test-three-networks duration="90m":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_hash="$(printf '%s' "${timestamp}-${duration}-three-networks-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    win_name="webcam-windy-${duration}-${run_hash}"
    fin_name="webcam-fintraffic-${duration}-${run_hash}"
    ska_name="webcam-skaping-${duration}-${run_hash}"
    maintenance_name="webcam-maintenance-${duration}-${run_hash}"

    docker compose --env-file .env --profile monitoring up -d \
        postgres mqtt prometheus grafana
    docker compose --env-file .env build webcam-job

    docker compose --env-file .env run -d --no-deps \
        --name "$win_name" --publish 127.0.0.1:8013:8013 \
        -e INGESTION_HEALTH_HOST=0.0.0.0 -e INGESTION_HEALTH_PORT=8013 \
        -e INGESTION_WORKER_THREADS=84 -e INGESTION_DATABASE_POOL_SIZE=64 \
        -e INGESTION_MAX_JOBS_PER_EPOCH=30000 \
        -e WINDY_MINIMUM_INGESTION_INTERVAL_S=300 \
        -e WINDY_MINIMUM_POLLING_INTERVAL_S=540 \
        -e WINDY_POLLING_INTERVAL_FACTOR=0.7 \
        -e WINDY_INGESTION_REQUEST_DELAY_S=0.01 \
        -e INGESTION_MIN_EPOCH_PERIOD_S=15 -e INGESTION_IDLE_DELAY_S=0 \
        -e INITIAL_STAGGER_WINDOW_S=600 \
        webcam-job python -m ingestion.worker --network win \
            --max-jobs 30000 --run-for-seconds "$run_seconds" \
            --stagger-initial-polling --batch-freshness --verbose

    docker compose --env-file .env run -d --no-deps \
        --name "$fin_name" --publish 127.0.0.1:8014:8014 \
        -e INGESTION_HEALTH_HOST=0.0.0.0 -e INGESTION_HEALTH_PORT=8014 \
        -e INGESTION_WORKER_THREADS=4 -e INGESTION_DATABASE_POOL_SIZE=8 \
        -e INGESTION_MAX_JOBS_PER_EPOCH=3000 \
        -e MINIMUM_INGESTION_INTERVAL_S=300 \
        -e FINTRAFFIC_MINIMUM_POLLING_INTERVAL_S=480 \
        -e POLLING_INTERVAL_FACTOR=0.7 \
        -e INGESTION_MIN_EPOCH_PERIOD_S=15 -e INGESTION_IDLE_DELAY_S=0 \
        -e INITIAL_STAGGER_WINDOW_S=600 \
        -e FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT=0 \
        -e FINTRAFFIC_DOWNLOAD_RETRY_COUNT=0 \
        webcam-job python -m ingestion.fintraffic.worker \
            --max-jobs 3000 --run-for-seconds "$run_seconds" \
            --stagger-initial-polling --verbose

    docker compose --env-file .env run -d --no-deps \
        --name "$ska_name" --publish 127.0.0.1:8015:8015 \
        -e INGESTION_HEALTH_HOST=0.0.0.0 -e INGESTION_HEALTH_PORT=8015 \
        -e INGESTION_WORKER_THREADS=2 -e INGESTION_DATABASE_POOL_SIZE=4 \
        -e INGESTION_MAX_JOBS_PER_EPOCH=100 \
        -e MINIMUM_INGESTION_INTERVAL_S=300 \
        -e SKAPING_MINIMUM_POLLING_INTERVAL_S=240 \
        -e POLLING_INTERVAL_FACTOR=0.7 \
        -e INGESTION_MIN_EPOCH_PERIOD_S=15 -e INGESTION_IDLE_DELAY_S=0 \
        -e INITIAL_STAGGER_WINDOW_S=600 \
        -e SKAPING_FRESHNESS_QUERY_RETRY_COUNT=0 \
        -e SKAPING_DOWNLOAD_RETRY_COUNT=0 \
        webcam-job python -m ingestion.skaping.worker \
            --max-jobs 100 --run-for-seconds "$run_seconds" \
            --stagger-initial-polling --verbose

    docker compose --env-file .env run -d --no-deps \
        --name "$maintenance_name" \
        --volume "$PWD/deployment/benchmarks:/benchmarks:ro" \
        webcam-job bash /benchmarks/run-quiet-maintenance 1200 3600 24

    docker update --cpus 6.0 "$win_name" >/dev/null
    docker update --cpus 0.5 "$fin_name" "$ska_name" >/dev/null
    docker update --cpus 0.5 "$maintenance_name" >/dev/null
    echo "Windy container:      $win_name (6 CPUs, 84 threads, 9-minute floor)"
    echo "Fintraffic container: $fin_name (0.5 CPU, 4 threads, 8-minute floor)"
    echo "Skaping container:    $ska_name (0.5 CPU, 2 threads, 4-minute floor)"
    echo "Maintenance container: $maintenance_name (0.5 CPU; cycles at T+20m and T+60m)"
    echo "Logs: docker logs -f <container-name>"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

# Checkpoint-13 recovery drill: full workers, Windy crash, PostgreSQL restart,
# then the cleanup-first production maintenance sequence. Runs in screen.
checkpoint13-unquiet-test duration="90m":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[mh]$ ]]; then
        echo "duration must be a positive number followed by m or h" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    if ((run_seconds < 3600)); then
        echo "checkpoint-13 unquiet test must last at least 60 minutes" >&2
        exit 2
    fi
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_hash="$(printf '%s' "${timestamp}-${duration}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="checkpoint13-unquiet-${duration}-${run_hash}"
    log="/tmp/${session}-${timestamp}.log"
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec deployment/benchmarks/run-checkpoint13-unquiet '$run_seconds'"
    echo "started screen session: $session"
    echo "log: $log"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

# Reset all learned periods, then exercise deterministic direct replacement.
period-replacement-test duration="30m" modulus="10":
    #!/usr/bin/env bash
    set -euo pipefail
    duration="$1"
    modulus="$2"
    if [[ ! "$duration" =~ ^[1-9][0-9]*[smh]$ ]]; then
        echo "duration must be a positive number followed by s, m, or h" >&2
        exit 2
    fi
    value="${duration::-1}"
    case "${duration: -1}" in
        s) run_seconds="$value" ;;
        m) run_seconds="$((value * 60))" ;;
        h) run_seconds="$((value * 3600))" ;;
    esac
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_hash="$(printf '%s' "${timestamp}-${duration}-${modulus}-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="period-replacement-${duration}-n${modulus}-${run_hash}"
    log="/tmp/${session}-${timestamp}.log"
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec deployment/benchmarks/run-period-replacement-test '$run_seconds' '$modulus'"
    echo "started screen session: $session"
    echo "log: $log"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

# Run all production-scope workers for 24 hours and trigger exactly one
# cleanup-first maintenance sequence at the next 00:00 UTC.
one-day-quiet-test:
    #!/usr/bin/env bash
    set -euo pipefail
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    run_hash="$(printf '%s' "${timestamp}-one-day-quiet-${BASHPID}-${RANDOM}" | sha256sum | cut -c1-8)"
    session="one-day-quiet-${run_hash}"
    log="/tmp/${session}-${timestamp}.log"
    screen -L -Logfile "$log" -dmS "$session" \
        bash -lc "cd '$PWD' && exec deployment/benchmarks/run-one-day-quiet"
    echo "started screen session: $session"
    echo "log: $log"
    echo "maintenance: once at the next 00:00 UTC"
    echo "period direct-replacement modulus: 250 for win, fin, and ska"
    echo "Grafana: tunnel local port 3000 to remote 127.0.0.1:3000"

# Stop all containers and remove their volumes (destructive: deletes local data)
destroy:
    docker compose --profile monitoring down --volumes
