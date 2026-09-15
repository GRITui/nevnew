"""Text extraction for document ingest (issue #79).

Kept deliberately small: PDF (the acceptance-criteria format — "Send a PDF
on Telegram") via pypdf, plus plain text/markdown pass-through. Anything
else (images, office docs, ...) is rejected with a clear error rather than
silently ingesting garbage — the Telegram photo path already has its own
vision-OCR flow (telegram-bot/bot.py `_run_vision_ocr`) and is not part of
this store.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

logger = logging.getLogger("nevnew-memory")

_TEXT_EXTENSIONS = (".txt", ".md", ".markdown")
_TEXT_CONTENT_TYPES = ("text/plain", "text/markdown")


class ExtractionError(Exception):
    """The uploaded file could not be turned into text (bad format,
    corrupt file, or a type we don't support yet)."""


def _looks_like_pdf(raw: bytes, filename: Optional[str], content_type: Optional[str]) -> bool:
    if raw[:5] == b"%PDF-":
        return True
    if content_type == "application/pdf":
        return True
    if filename and filename.lower().endswith(".pdf"):
        return True
    return False


def _looks_like_text(filename: Optional[str], content_type: Optional[str]) -> bool:
    if content_type in _TEXT_CONTENT_TYPES:
        return True
    if filename and filename.lower().endswith(_TEXT_EXTENSIONS):
        return True
    return False


def _extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency always pinned in requirements.txt
        raise ExtractionError("PDF support requires the 'pypdf' package") from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise ExtractionError(f"could not open PDF: {exc}") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ExtractionError(f"PDF is password-protected: {exc}") from exc

    pages = []
    for index, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 — one bad page must not fail the whole file
            logger.warning("PDF page %d text extraction failed: %s", index, exc)
            page_text = ""
        if page_text.strip():
            pages.append(page_text.strip())
    text = "\n\n".join(pages).strip()
    if not text:
        raise ExtractionError(
            "no extractable text found in PDF (it may be a scanned image "
            "with no text layer — OCR is not supported for document ingest)"
        )
    return text


def extract_text(raw: bytes, filename: Optional[str], content_type: Optional[str]) -> str:
    """Extract plain text from an uploaded file's raw bytes.

    Raises ExtractionError for unsupported/corrupt files.
    """
    if not raw:
        raise ExtractionError("file is empty")

    if _looks_like_pdf(raw, filename, content_type):
        return _extract_pdf_text(raw)

    if _looks_like_text(filename, content_type):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                raise ExtractionError(f"could not decode text file: {exc}") from exc

    # Fall back: sniff for UTF-8 plain text with no extension/content-type
    # hint (e.g. a bare paste saved without an extension).
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractionError(
            f"unsupported file type (filename={filename!r}, content_type={content_type!r}); "
            "supported: PDF, .txt, .md"
        ) from exc
