# =============================================================================
# TrackFlow API — Development container
#
# Build context must be the repository root so that both
#   services/   (FastAPI application code)
#   scripts/    (incident-analysis package)
#   pipelines/  (reporting validation and shared pipeline contracts)
# are available inside the image.
#
# Build:
#   docker build -f services/Dockerfile -t trackflow-api:dev .
#
# Run:
#   docker run -it --rm -p 8000:8000 \
#     -e JWT_SECRET_KEY="dev-secret" \
#     -e DATABASE_URL="..." \
#     trackflow-api:dev
# =============================================================================

# ── Stage 0 — Base Python image ─────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Install uv (fast Python package installer) ──────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# ── Install dependencies (cached layer) ─────────────────────────────────────
COPY services/api/requirements.txt /app/services/api/requirements.txt
RUN uv pip install --system --no-cache -r /app/services/api/requirements.txt

# ── Copy application code ───────────────────────────────────────────────────
# Only the directories that the runtime needs:
#   services/   — FastAPI application code
#   scripts/    — Incident-analysis package (imported by services.api.main)
#   agents/     — compiled customer-support LangGraph
COPY services/ /app/services/
COPY scripts/ /app/scripts/
COPY pipelines/ /app/pipelines/
COPY data/pipelines/rfp_intake/ /app/data/pipelines/rfp_intake/
COPY agents/ /app/agents/

# API and worker share the named RFP volume under the same numeric identity.
# Docker copies this ownership into a newly-created empty named volume.
RUN rm -rf /app/services/api/data \
    && groupadd --gid 10002 trackflow-api \
    && useradd --uid 10002 --gid trackflow-api --create-home --shell /usr/sbin/nologin trackflow-api \
    && mkdir -p /app/services/api/data /app/data/raw/rfp_intake \
    && chown -R 10002:10002 /app/services/api/data /app/data/raw/rfp_intake

USER 10002:10002

# ── Runtime configuration ───────────────────────────────────────────────────
EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
