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
#     -e JWT_SECRET_KEY \
#     -e DATABASE_URL="..." \
#     trackflow-api:dev
# =============================================================================

# ── Stage 0 — Base Python image ─────────────────────────────────────────────
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Install uv (fast Python package installer) ──────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /uvx /bin/

# ── Install dependencies (cached layer) ─────────────────────────────────────
COPY services/api/requirements.lock /app/services/api/requirements.lock
RUN uv --no-config pip install --system --no-cache --require-hashes --only-binary :all: \
    --default-index https://pypi.org/simple -r /app/services/api/requirements.lock

# ── Copy application code ───────────────────────────────────────────────────
# Only the directories that the runtime needs:
#   services/   — FastAPI application code
#   scripts/    — Incident-analysis package (imported by services.api.main)
#   agents/     — compiled customer-support LangGraph
COPY services/ /app/services/
COPY scripts/ /app/scripts/
COPY pipelines/ /app/pipelines/
COPY data/pipelines/rfp_intake/ /app/data/pipelines/rfp_intake/
COPY data/pipelines/rfp_response/ /app/data/pipelines/rfp_response/
COPY agents/ /app/agents/

# API and worker share the named RFP volume under the same numeric identity.
# Docker copies this ownership into a newly-created empty named volume.
RUN rm -rf /app/services/api/data \
    && groupadd --gid 10002 trackflow-api \
    && useradd --uid 10002 --gid trackflow-api --create-home --shell /usr/sbin/nologin trackflow-api \
    && mkdir -p /app/services/api/data /app/data/raw/rfp_intake \
    && chown -R 10002:10002 /app/services/api/data /app/data/raw/rfp_intake \
    && install -d -m 0700 -o 10002 -g 10002 /var/lib/trackflow/rfp-approval

USER 10002:10002

# ── Runtime configuration ───────────────────────────────────────────────────
EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
