# syntax=docker/dockerfile:1

# Build the pinned scheduler binary with Go's module checksum verification.
FROM golang:1.23-alpine AS supercronic-builder

ARG SUPERCRONIC_VERSION=v0.2.33
RUN CGO_ENABLED=0 GOBIN=/out go install github.com/aptible/supercronic@${SUPERCRONIC_VERSION}


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_CACHE=1 \
    PREFECT_SERVER_UI_ENABLED=false \
    PREFECT_SERVER_UI_V2_ENABLED=false \
    PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS=120 \
    TZ=UTC \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=supercronic-builder /out/supercronic /usr/local/bin/supercronic
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

# Install the complete Python runtime declared by the project, including
# Prefect, before dropping privileges. Only scheduler runtime sources follow.
COPY pyproject.toml /app/pyproject.toml
COPY uv.lock /app/uv.lock
COPY pipelines/ /app/pipelines/
COPY scripts/ /app/scripts/
# The build bind makes service source available without placing API runtime
# data in a COPY layer. The scheduler needs service modules, never auth.json or
# other mutable service data.
RUN --mount=type=bind,source=services,target=/source-services,ro \
    cp -a /source-services /app/services \
    && rm -rf /app/services/api/data \
    && uv sync --frozen --no-dev \
    && groupadd --gid 10001 trackflow \
    && useradd --uid 10001 --gid trackflow --create-home --shell /usr/sbin/nologin trackflow \
    && mkdir -p /app/data/raw /app/infra/scheduler /app/services/api/data \
    && chown -R trackflow:trackflow \
        /app/data/raw /app/infra/scheduler /app/services/api/data

COPY --chown=trackflow:trackflow infra/scheduler/crontab /app/infra/scheduler/crontab

USER 10001:10001

CMD ["/usr/local/bin/supercronic", "/app/infra/scheduler/crontab"]
