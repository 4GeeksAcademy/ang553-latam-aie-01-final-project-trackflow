"""Bounded PDF-to-Markdown conversion for RFP intake."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Protocol

from data.pipelines.rfp_intake.contracts import ConversionMetrics


class PDFConversionError(RuntimeError):
    """The source could not be converted safely into usable text."""


@dataclass(frozen=True)
class ConverterOutput:
    markdown: str
    page_count: int
    pages_with_text: int


class MarkdownConverter(Protocol):
    name: str
    version: str

    def convert(
        self, path: Path, *, max_pages: int, timeout_seconds: float
    ) -> ConverterOutput: ...


class MarkItDownConverter:
    """Run the parser in a killable process; threads cannot enforce a hard timeout."""

    name = "markitdown"

    @property
    def version(self) -> str:
        try:
            return importlib.metadata.version("markitdown")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def convert(
        self, path: Path, *, max_pages: int, timeout_seconds: float
    ) -> ConverterOutput:
        with tempfile.TemporaryDirectory(prefix="trackflow-rfp-") as temp_dir:
            output_path = Path(temp_dir) / "converted.md"
            command = [
                sys.executable,
                "-m",
                "data.pipelines.rfp_intake.pdf_worker",
                str(path),
                str(output_path),
                str(max_pages),
            ]
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PDFConversionError("PDF conversion timed out") from exc
            if result.returncode != 0:
                detail = result.stderr.strip() or "PDF conversion failed"
                raise PDFConversionError(detail)
            try:
                metrics = json.loads(result.stdout)
                markdown = output_path.read_text(encoding="utf-8")
                return ConverterOutput(
                    markdown=markdown,
                    page_count=int(metrics["page_count"]),
                    pages_with_text=int(metrics["pages_with_text"]),
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PDFConversionError("PDF converter returned an invalid result") from exc


@dataclass(frozen=True)
class PDFConversionResult:
    markdown: str
    metrics: ConversionMetrics


_PDF_SIGNATURE = b"%PDF-"


def pdf_to_markdown(
    path: str | Path,
    *,
    markdown_reference: str,
    max_pages: int,
    timeout_seconds: float,
    converter: MarkdownConverter | None = None,
) -> PDFConversionResult:
    """Convert a server-controlled PDF under explicit page and wall-clock bounds."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    source = Path(path)
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise PDFConversionError("PDF source is unavailable") from exc
    if not content.startswith(_PDF_SIGNATURE):
        raise PDFConversionError("Source is not a PDF")
    selected = converter or MarkItDownConverter()
    converted = selected.convert(
        source, max_pages=max_pages, timeout_seconds=timeout_seconds
    )
    if converted.page_count < 1 or converted.page_count > max_pages:
        raise PDFConversionError("PDF page limit exceeded")
    if not 0 <= converted.pages_with_text <= converted.page_count:
        raise PDFConversionError("PDF parser returned invalid page metrics")
    markdown = converted.markdown.strip()
    if not markdown:
        raise PDFConversionError("PDF produced empty Markdown")
    return PDFConversionResult(
        markdown=markdown,
        metrics=ConversionMetrics(
            sha256=hashlib.sha256(content).hexdigest(),
            bytes=len(content),
            page_count=converted.page_count,
            pages_with_text=converted.pages_with_text,
            markdown_reference=markdown_reference,
            extracted_characters=len(markdown),
            markdown_characters=len(markdown),
            converter=selected.name,
            converter_version=selected.version,
        ),
    )
