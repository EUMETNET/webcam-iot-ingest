FROM postgres:16.9-bookworm AS postgres-client

FROM python:3.14-slim-bookworm

SHELL ["/bin/bash", "-eux", "-o", "pipefail", "-c"]

ENV DOCKER_PATH="/app"


COPY "./api" "${DOCKER_PATH}/api/"
COPY "./config" "${DOCKER_PATH}/config/"
COPY "./database" "${DOCKER_PATH}/database/"
COPY "./discovery" "${DOCKER_PATH}/discovery/"
COPY "./ingestion" "${DOCKER_PATH}/ingestion/"
COPY "./storage" "${DOCKER_PATH}/storage/"
COPY "pyproject.toml" "${DOCKER_PATH}/"
COPY "README.md" "${DOCKER_PATH}/"
COPY --from=postgres-client /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump

WORKDIR "${DOCKER_PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends --yes libpq5 \
    && rm -rf /var/lib/apt/lists/*

# hadolint ignore=DL3013
RUN pip install --no-cache-dir . \
    && mkdir -p /tmp/metrics

ENV PROMETHEUS_MULTIPROC_DIR=/tmp/metrics

CMD ["gunicorn", "api.main:app", "--worker-class=uvicorn.workers.UvicornWorker"]
