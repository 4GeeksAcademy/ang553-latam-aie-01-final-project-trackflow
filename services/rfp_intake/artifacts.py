"""Safe streaming persistence for untrusted RFP uploads."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile


class InvalidRFPArtifact(ValueError):
    pass


async def persist_pdf(upload: UploadFile, *, root: Path, ticket_id: UUID, max_bytes: int) -> tuple[str, str, int]:
    if Path(upload.filename or "").suffix.lower() != ".pdf":
        raise InvalidRFPArtifact("a PDF file is required")
    root_resolved, target_dir = root.resolve(), (root / str(ticket_id)).resolve()
    if root_resolved not in target_dir.parents:
        raise InvalidRFPArtifact("invalid artifact path")
    target_dir.mkdir(parents=True, exist_ok=False)
    temporary, destination = target_dir / ".source.pdf.tmp", target_dir / "source.pdf"
    digest, size, prefix = hashlib.sha256(), 0, b""
    try:
        with temporary.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise InvalidRFPArtifact("PDF exceeds configured upload limit")
                if len(prefix) < 5:
                    prefix += chunk[: 5 - len(prefix)]
                digest.update(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if size == 0 or prefix != b"%PDF-":
            raise InvalidRFPArtifact("invalid PDF signature")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True); destination.unlink(missing_ok=True)
        try: target_dir.rmdir()
        except OSError: pass
        raise
    return f"rfp_intake/{ticket_id}/source.pdf", digest.hexdigest().upper(), size


def cleanup_artifact(root: Path, ticket_id: UUID) -> None:
    root_resolved, target_dir = root.resolve(), (root / str(ticket_id)).resolve()
    if root_resolved not in target_dir.parents:
        return
    for name in (".source.pdf.tmp", "source.pdf", "source.md"):
        (target_dir / name).unlink(missing_ok=True)
    try: target_dir.rmdir()
    except OSError: pass


def persist_markdown(*, root: Path, ticket_id: UUID, markdown: str) -> str:
    """Atomically persist derived Markdown under the server-controlled ticket dir."""
    root_resolved, target_dir = root.resolve(), (root / str(ticket_id)).resolve()
    if root_resolved not in target_dir.parents or not target_dir.is_dir():
        raise InvalidRFPArtifact("RFP artifact directory is unavailable")
    temporary, destination = target_dir / ".source.md.tmp", target_dir / "source.md"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(markdown)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return f"rfp_intake/{ticket_id}/source.md"
