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
    uv run python -m database.healthcheck

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
    if [[ "$(just --version)" != "just {{just_version}}" ]]; then
        echo "this repository requires just {{just_version}}" >&2
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
    if [[ "$scope" != "all_windy" ]]; then
        countries=(--countries "${scope^^}")
    fi
    if [[ "$mode" == "staggered" ]]; then
        stagger=(--stagger-initial-polling)
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
            "${countries[@]}"

# Stop all containers and remove their volumes (destructive: deletes local data)
destroy:
    docker compose --profile monitoring down --volumes
