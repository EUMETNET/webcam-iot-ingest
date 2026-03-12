FROM python:3.14-slim-bookworm

SHELL ["/bin/bash", "-eux", "-o", "pipefail", "-c"]

ENV DOCKER_PATH="/app"


COPY "./api" "${DOCKER_PATH}/api/"
COPY "pyproject.toml" "${DOCKER_PATH}/"

WORKDIR "${DOCKER_PATH}"

# hadolint ignore=DL3013
RUN pip install --no-cache-dir . \
    && mkdir -p /tmp/metrics

ENV PROMETHEUS_MULTIPROC_DIR=/tmp/metrics

CMD ["gunicorn", "api.main:app", "--worker-class=uvicorn.workers.UvicornWorker"]