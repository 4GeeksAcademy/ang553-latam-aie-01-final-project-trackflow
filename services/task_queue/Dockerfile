# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_CACHE=1 \
    PREFECT_SERVER_UI_ENABLED=false \
    PREFECT_SERVER_UI_V2_ENABLED=false \
    PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS=120 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock /app/
COPY pipelines/ /app/pipelines/
COPY data/pipelines/rfp_intake/ /app/data/pipelines/rfp_intake/
COPY data/pipelines/rfp_response/ /app/data/pipelines/rfp_response/
COPY docs/company-knowledge-base/ /app/docs/company-knowledge-base/
COPY services/ /app/services/
COPY scripts/ /app/scripts/

RUN rm -rf /app/services/api/data \
    && uv sync --frozen --no-dev \
    && groupadd --gid 10002 trackflow-tasks \
    && useradd --uid 10002 --gid trackflow-tasks --create-home --shell /usr/sbin/nologin trackflow-tasks \
    && mkdir -p /app/services/api/data /app/data/raw/rfp_intake \
    && chown -R 10002:10002 /app/services/api/data /app/data/raw/rfp_intake

USER 10002:10002

CMD ["celery", "--app", "services.task_queue.celery_app:celery_app", "worker", "--loglevel=INFO", "--events"]
