"""Isolated PDF parser process used by :mod:`pdf_conversion`."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer


def _page_metrics(source: Path, max_pages: int) -> tuple[int, int]:
    page_count = 0
    pages_with_text = 0
    for layout in extract_pages(str(source)):
        page_count += 1
        if page_count > max_pages:
            raise ValueError("PDF page limit exceeded")
        if any(
            isinstance(item, LTTextContainer) and item.get_text().strip()
            for item in layout
        ):
            pages_with_text += 1
    if page_count == 0:
        raise ValueError("PDF contains no pages")
    return page_count, pages_with_text


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    source, output = Path(argv[1]), Path(argv[2])
    try:
        max_pages = int(argv[3])
        page_count, pages_with_text = _page_metrics(source, max_pages)
        from markitdown import MarkItDown

        result = MarkItDown().convert(str(source))
        markdown = getattr(result, "text_content", None)
        if not isinstance(markdown, str):
            raise ValueError("PDF converter returned no text")
        output.write_text(markdown, encoding="utf-8")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"page_count": page_count, "pages_with_text": pages_with_text}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
