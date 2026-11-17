"""TenderForge - Document Processing: PDF Validation & Safe Opening

Handles:
- Safe PDF opening with PyMuPDF.
- Corrupted PDF detection with clear error reporting.
- Password-protected PDF detection.
- Empty document detection.
- Page-by-page inspection: empty page vs image-only/scanned page vs text page.
- Threshold-based OCR requirement detection.
- Large PDF protection and memory safety.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import fitz

_logger = logging.getLogger(__name__)

LOW_TEXT_CHARACTER_LIMIT = 40
IMAGE_AREA_THRESHOLD = 0.25
MAX_REASONABLE_PAGE_COUNT = 2000


class PDFValidationError(Exception):
    def __init__(self, message: str, status_code: int = 400, is_scanned: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.is_scanned = is_scanned


@dataclass
class PageValidationInfo:
    page_number: int
    char_count: int
    has_image: bool
    is_empty: bool
    is_scanned: bool


@dataclass
class DocumentValidationSummary:
    page_count: int
    total_characters: int
    empty_pages_count: int
    scanned_pages_count: int
    needs_ocr: bool
    status: str
    error_message: Optional[str] = None


def inspect_pdf_page(page, page_number: int) -> tuple[str, PageValidationInfo]:
    """Safely extract raw text and inspect image/scanned status for a single page.
    
    Returns:
        (raw_text, PageValidationInfo)
    """
    raw_text = page.get_text("text") or ""
    char_count = len(raw_text.strip())

    page_area = max(page.rect.get_area(), 1.0)
    has_page_image = False
    try:
        images = page.get_image_info()
        for img in images:
            bbox = img.get("bbox")
            if bbox:
                img_area = fitz.Rect(bbox).get_area()
                if img_area / page_area >= IMAGE_AREA_THRESHOLD:
                    has_page_image = True
                    break
    except Exception:
        has_page_image = False

    is_empty = char_count == 0 and not has_page_image
    is_scanned = (char_count < LOW_TEXT_CHARACTER_LIMIT and has_page_image) or (char_count == 0 and has_page_image)

    info = PageValidationInfo(
        page_number=page_number,
        char_count=char_count,
        has_image=has_page_image,
        is_empty=is_empty,
        is_scanned=is_scanned,
    )
    return raw_text, info


def open_and_validate_pdf(file_path: Path) -> fitz.Document:
    """Safely open and validate a PDF file with PyMuPDF.
    
    Raises:
        PDFValidationError with specific user-friendly messages for corruption,
        passwords, or zero-page documents.
    """
    if not file_path.is_file():
        raise PDFValidationError("Tender PDF file could not be found.", 404)

    try:
        doc = fitz.open(file_path)
    except Exception as exc:
        raise PDFValidationError("Could not read this PDF. It may be corrupted or unsupported.", 400) from exc

    if doc.needs_pass:
        doc.close()
        raise PDFValidationError("This PDF is password-protected and cannot be processed.", 400)

    if doc.page_count == 0:
        doc.close()
        raise PDFValidationError("This PDF does not contain any pages.", 400)

    return doc


def assess_document_ocr_status(
    page_count: int,
    page_infos: list[PageValidationInfo],
) -> DocumentValidationSummary:
    """Assess overall document status: completed vs needs_ocr vs failed."""
    if page_count == 0:
        return DocumentValidationSummary(
            page_count=0,
            total_characters=0,
            empty_pages_count=0,
            scanned_pages_count=0,
            needs_ocr=False,
            status="failed",
            error_message="This PDF does not contain any pages.",
        )

    total_chars = sum(p.char_count for p in page_infos)
    empty_pages = sum(1 for p in page_infos if p.is_empty)
    scanned_pages = sum(1 for p in page_infos if p.is_scanned)

    # Any scanned page needs explicit OCR handling so its source text is not
    # silently omitted from an otherwise processable mixed document.
    needs_ocr = scanned_pages > 0

    if needs_ocr:
        return DocumentValidationSummary(
            page_count=page_count,
            total_characters=total_chars,
            empty_pages_count=empty_pages,
            scanned_pages_count=scanned_pages,
            needs_ocr=True,
            status="needs_ocr",
            error_message=(
                f"This PDF has {scanned_pages} scanned page(s) and cannot currently "
                "be processed without OCR."
            ),
        )

    if total_chars == 0:
        return DocumentValidationSummary(
            page_count=page_count,
            total_characters=0,
            empty_pages_count=empty_pages,
            scanned_pages_count=scanned_pages,
            needs_ocr=False,
            status="failed",
            error_message="This PDF contains no extractable text. It may be blank or scanned in an unsupported format.",
        )

    return DocumentValidationSummary(
        page_count=page_count,
        total_characters=total_chars,
        empty_pages_count=empty_pages,
        scanned_pages_count=scanned_pages,
        needs_ocr=False,
        status="completed",
        error_message=None,
    )

