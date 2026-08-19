"""
TrackFlow Incident Analysis API — entry point.

Run with::

    uvicorn services.api.main:app --reload

"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from scripts.incidents.analyzer import analyze_records, export_results_csv, load_csv
from scripts.incidents import CsvLoadError
from services.api.auth_security import get_current_user
from services.api.routes.auth import router as auth_router
from services.api.routes.profiles import router as profiles_router
from services.api.routes.suppliers import router as suppliers_router
from services.api.routes.users import router as users_router

# ── CORS (development) ──────────────────────────────────────────────────────
#
# Allow the Next.js dev server (localhost:3000) to call this API directly.
# These origins are explicit — no wildcard — and only cover local development.
# In production, replace with the actual frontend domain(s).

_DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Allow this repository's current Codespace frontend origin on port 3000
# when running in GitHub Codespaces.
_codespace_name = os.getenv("CODESPACE_NAME")
_codespaces_domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")

if _codespace_name and _codespaces_domain:
    _DEV_ORIGINS.append(
        f"https://{_codespace_name}-3000.{_codespaces_domain}"
    )

app = FastAPI(
    title="TrackFlow Incident Analysis API",
    version="0.2.0",
    description=(
        "Incident validation, aggregation, and export for the TrackFlow "
        "logistics platform. "
        "Business logic is provided by the ``scripts.incidents`` package."
    ),
)

# ── Allowed file extensions for upload validation ────────────────────────────

_ALLOWED_EXTENSIONS = frozenset({".csv"})

# ── In-memory storage for the last successful analysis ───────────────────────
#
# NOTE: This module-level variable is intentionally simple.
# Limitations:
#   - Lost on server restart.
#   - Not shared across multiple workers/processes.
#   - Suitable only for development and single‑worker deployments.
# Future iterations may replace this with persistent storage (DB, Redis, etc.).
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEV_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

_last_result: dict | None = None

app.include_router(suppliers_router)
app.include_router(
    suppliers_router,
    prefix="/api",
    include_in_schema=False,
)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(profiles_router)


# ── Health check ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a simple status ping to confirm the service is running."""
    return {"status": "ok"}


# ── Analyze endpoint ─────────────────────────────────────────────────────────


@app.post(
    "/api/incidents/analyze",
    dependencies=[Depends(get_current_user)],
)
async def analyze_incidents(file: UploadFile | None = File(None)) -> dict:
    """
    Upload a TrackFlow incident CSV and receive aggregate analysis results.

    The CSV must contain all required columns (see ``scripts.incidents``).
    Returns JSON with the same structure as ``analyze_records()``.
    """
    # ── Validate file presence ──
    if file is None:
        raise HTTPException(
            status_code=400,
            detail="No file provided. Send a CSV file as multipart/form-data.",
        )

    if file.filename is None or file.filename.strip() == "":
        raise HTTPException(
            status_code=400,
            detail="No file provided. Send a CSV file as multipart/form-data.",
        )

    # ── Validate file extension ──
    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{suffix}'. "
                "Only CSV files are accepted."
            ),
        )

    # ── Read content ──
    content = await file.read()

    if not content or len(content) == 0:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty.",
        )

    # ── Write to temp file, process, clean up ──
    # load_csv() expects a file path, so we bridge via a temporary file.
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        records = load_csv(tmp_path)
        result = analyze_records(records)

        # Store the aggregate result for later export.  Only on success.
        global _last_result  # noqa: PLW0603
        _last_result = result
    except CsvLoadError as e:
        raise HTTPException(
            status_code=422,
            detail=str(e),
        )
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Internal server error while processing the file.",
        )
    finally:
        # Best-effort cleanup of the temp file
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    return result


# ── Export endpoint (last successful analysis) ───────────────────────────────


@app.get(
    "/api/incidents/results/export",
    dependencies=[Depends(get_current_user)],
)
async def export_results() -> Response:
    """
    Download the last successful analysis as a CSV file.

    Returns ``section,metric,value`` rows with aggregate metrics only.
    No individual records, PII, or sensitive data are included.
    """
    global _last_result  # noqa: PLW0603

    if _last_result is None:
        raise HTTPException(
            status_code=404,
            detail="No analysis results available. "
            "Submit a CSV via POST /api/incidents/analyze first.",
        )

    # Use the re-usable export function to generate CSV content in a temp file
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".csv", mode="w", encoding="utf-8", delete=False
        ) as tmp:
            tmp_path = tmp.name

        export_results_csv(_last_result, tmp_path)

        with open(tmp_path, encoding="utf-8") as f:
            csv_content = f.read()
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="results.csv"'
        },
    )