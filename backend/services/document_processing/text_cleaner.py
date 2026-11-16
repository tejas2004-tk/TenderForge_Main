"""TenderForge - Document Processing: Text Cleaning & Normalization

Deterministic Python rules for:
- Conservative whitespace normalization.
- Broken line-break correction (hyphenation and soft wraps).
- Paragraph and sentence boundary preservation.
- Repeated header/footer detection across pages.
- Page-number noise detection and removal.
- Unnecessary extraction artefact removal.
- Multi-column layout detection and sorting.
- Reliable table extraction to Markdown tables.
- Guaranteed preservation of numbers, dates, percentages, currencies, and units.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple

# Regex patterns for page numbering noise
_PAGE_NUMBER_PATTERNS = [
    re.compile(r"^\s*(?:page|pg\.?)\s*[-:]?\s*\d+\s*(?:of|\/|-)\s*\d+\s*$", re.I),
    re.compile(r"^\s*(?:page|pg\.?)\s*[-:]?\s*\d+\s*$", re.I),
    re.compile(r"^\s*-\s*\d+\s*-\s*$"),
    re.compile(r"^\s*\[\s*\d+\s*\]\s*$"),
    re.compile(r"^\s*\d+\s*(?:of|\/)\s*\d+\s*$", re.I),
    re.compile(r"^\s*p\.\s*\d+\s*$", re.I),
]

# Prefixes that should retain a hyphen when split across a line break
_KEEP_HYPHEN_PREFIXES = {
    "pre", "non", "sub", "anti", "multi", "semi", "cross", "self",
    "all", "inter", "intra", "co", "ex", "post", "quasi", "re",
}

# Regex to detect list item / bullet starts
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)*[.)]\s+"          # 1. or 1.1 or 1)
    r"|[a-zA-Z][.)]\s+"              # a. or a) or A. or A)
    r"|\([0-9a-zA-ZivxIVX]+\)\s+"    # (1) or (a) or (i)
    r"|[ivxIVX]+[.)]\s+"             # i. or ii) or IV.
    r"|[•\-\*–—§▪▫►▶]\s+"            # Bullet points
    r")"
)

# Regex to detect section headings (short uppercase lines or numbered headings)
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:SECTION|PART|CHAPTER|CLAUSE|ANNEXURE|APPENDIX|SCHEDULE|FORM|VOLUME)\s+[\dA-Z.-]+"
    r"|[A-Z0-9\s.,:&/()'\"]{3,70}$"
    r")",
    re.M,
)

# Punctuation that typically terminates a sentence or clause
_SENTENCE_TERMINATORS = {".", "!", "?", ":", ";"}


@dataclass
class CleanedPageResult:
    """Result of cleaning a single page."""
    page_number: int
    raw_text: str
    cleaned_text: str
    char_count: int
    has_table: bool = False
    table_count: int = 0
    has_multicolumn: bool = False
    line_fixes_count: int = 0
    removed_header_footer_count: int = 0
    cleaning_metadata: dict[str, Any] = field(default_factory=dict)


def normalize_whitespace_and_artefacts(text: str) -> str:
    """Normalize irregular whitespace, control characters, and unicode ligatures.
    
    Preserves newlines for paragraph and line boundaries.
    """
    if not text:
        return ""

    # Normalize unicode (ligatures like fi, fl -> f i, f l)
    text = unicodedata.normalize("NFKC", text)

    # Remove non-printable control characters, null bytes, form feeds, but keep \n and \t
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]", "", text)

    # Replace zero-width spaces, non-breaking spaces, hair spaces with regular space
    text = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff]", "", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")

    # Collapse consecutive horizontal whitespace (spaces/tabs) to a single space
    lines = []
    for line in text.split("\n"):
        cleaned_line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(cleaned_line)

    return "\n".join(lines)


def is_page_number_noise(line: str) -> bool:
    """Detect whether a line consists solely of page numbering noise."""
    line_clean = line.strip()
    if not line_clean:
        return False
    for pattern in _PAGE_NUMBER_PATTERNS:
        if pattern.fullmatch(line_clean):
            return True
    # Isolated integer of 1-4 digits
    if re.fullmatch(r"\d{1,4}", line_clean):
        return True
    return False


def detect_repeated_headers_and_footers(
    pages_raw_lines: list[list[str]],
    min_pages: int = 3,
    min_ratio: float = 0.5,
) -> tuple[set[str], set[str]]:
    """Detect repeated header and footer lines across pages in a document.
    
    Args:
        pages_raw_lines: List of lines for each page.
        min_pages: Minimum number of pages a header/footer must appear on.
        min_ratio: Minimum fraction of total pages.
        
    Returns:
        (detected_headers, detected_footers) sets of normalized line strings.
    """
    total_pages = len(pages_raw_lines)
    if total_pages < 2:
        return set(), set()

    # Threshold for detection
    threshold = max(2 if total_pages <= 3 else min_pages, int(total_pages * min_ratio))

    header_candidates = Counter()
    footer_candidates = Counter()

    for lines in pages_raw_lines:
        non_empty = [l.strip() for l in lines if l.strip()]
        if not non_empty:
            continue

        # Look at the first 3 non-empty lines for headers
        top_lines = non_empty[:3]
        for line in top_lines:
            # Skip if line is purely page numbering noise
            if is_page_number_noise(line):
                continue
            # Strip variable numbers (like dynamic page numbers embedded in headers)
            normalized = re.sub(r"\bpage\s+\d+\b", "page #", line.casefold())
            normalized = re.sub(r"\b\d+/\d+\b", "#/#", normalized)
            if len(normalized) >= 5:
                header_candidates[normalized] += 1

        # Look at the last 3 non-empty lines for footers
        bottom_lines = non_empty[-3:]
        for line in bottom_lines:
            if is_page_number_noise(line):
                continue
            normalized = re.sub(r"\bpage\s+\d+\b", "page #", line.casefold())
            normalized = re.sub(r"\b\d+/\d+\b", "#/#", normalized)
            if len(normalized) >= 5:
                footer_candidates[normalized] += 1

    detected_headers = {line for line, count in header_candidates.items() if count >= threshold}
    detected_footers = {line for line, count in footer_candidates.items() if count >= threshold}

    return detected_headers, detected_footers


def _matches_detected_header_footer(line: str, detected_set: set[str]) -> bool:
    """Check if a line matches any of the detected repeated headers/footers."""
    normalized = re.sub(r"\bpage\s+\d+\b", "page #", line.strip().casefold())
    normalized = re.sub(r"\b\d+/\d+\b", "#/#", normalized)
    return normalized in detected_set


def fix_broken_linebreaks_and_hyphens(lines: list[str]) -> tuple[str, int]:
    """Join broken lines and de-hyphenate words while preserving paragraph/list boundaries.
    
    Returns:
        (joined_text, line_fixes_count)
    """
    if not lines:
        return "", 0

    # Pass 1: De-hyphenate words broken across line ends
    working_lines: list[str] = [l for l in lines]
    line_fixes = 0
    idx = 0

    while idx < len(working_lines):
        current = working_lines[idx].strip()
        hyphen_match = re.search(r"(\b[a-zA-Z]{2,})-\s*$", current)
        if hyphen_match and idx + 1 < len(working_lines):
            next_line = working_lines[idx + 1].strip()
            next_word_match = re.match(r"^([a-zA-Z]{2,}\b)", next_line)
            if next_word_match:
                prefix = hyphen_match.group(1)
                suffix = next_word_match.group(1)
                if prefix.lower() in _KEEP_HYPHEN_PREFIXES:
                    joined_word = f"{prefix}-{suffix}"
                else:
                    joined_word = f"{prefix}{suffix}"

                current_prefix = current[:hyphen_match.start(1)]
                next_remainder = next_line[next_word_match.end(1):].lstrip()

                working_lines[idx] = f"{current_prefix}{joined_word}"
                working_lines[idx + 1] = next_remainder
                line_fixes += 1
        idx += 1

    # Pass 2: Soft wrap line joins within paragraphs
    output_lines: list[str] = []
    idx = 0

    while idx < len(working_lines):
        current = working_lines[idx].strip()

        if not current:
            output_lines.append("")
            idx += 1
            continue

        can_join = False
        if idx + 1 < len(working_lines):
            next_line = working_lines[idx + 1].strip()
            if next_line:
                current_ends_punct = current[-1] in _SENTENCE_TERMINATORS or current.endswith("—")
                is_table_row = current.startswith("|") or current.endswith("|") or next_line.startswith("|")
                next_is_list = bool(_LIST_ITEM_RE.match(next_line))
                next_is_heading = bool(_HEADING_RE.match(next_line)) and len(next_line) < 60 and next_line.isupper()

                if not current_ends_punct and not is_table_row and not next_is_list and not next_is_heading:
                    can_join = True

        if can_join:
            next_line = working_lines[idx + 1].strip()
            current = f"{current} {next_line}"
            line_fixes += 1
            idx += 2
            while idx < len(working_lines):
                peek_line = working_lines[idx].strip()
                if not peek_line:
                    break
                c_ends = current[-1] in _SENTENCE_TERMINATORS
                is_tbl = current.startswith("|") or peek_line.startswith("|")
                p_list = bool(_LIST_ITEM_RE.match(peek_line))
                p_head = bool(_HEADING_RE.match(peek_line)) and len(peek_line) < 60 and peek_line.isupper()
                if not c_ends and not is_tbl and not p_list and not p_head:
                    current = f"{current} {peek_line}"
                    line_fixes += 1
                    idx += 1
                else:
                    break
            output_lines.append(current)
        else:
            output_lines.append(current)
            idx += 1

    # Clean up multiple consecutive empty lines to maximum 1 (denoting paragraph break)
    final_paragraphs = []
    prev_empty = False
    for line in output_lines:
        if not line:
            if not prev_empty:
                final_paragraphs.append("")
                prev_empty = True
        else:
            final_paragraphs.append(line)
            prev_empty = False

    return "\n".join(final_paragraphs), line_fixes


def format_table_as_markdown(table_data: list[list[Optional[str]]]) -> str:
    """Format raw 2D table data into a clean, deterministic Markdown table."""
    if not table_data or not table_data[0]:
        return ""

    # Clean up cell values
    cleaned_rows: list[list[str]] = []
    for row in table_data:
        cleaned_row = []
        for cell in row:
            text = (cell or "").replace("\n", " ").strip()
            # Escape pipe characters inside cells
            text = text.replace("|", "\\|")
            cleaned_row.append(text)
        cleaned_rows.append(cleaned_row)

    if not cleaned_rows:
        return ""

    num_cols = max(len(row) for row in cleaned_rows)
    if num_cols == 0:
        return ""

    # Normalize row lengths
    for row in cleaned_rows:
        while len(row) < num_cols:
            row.append("")

    header = cleaned_rows[0]
    separator = ["---"] * num_cols

    md_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]

    for row in cleaned_rows[1:]:
        md_lines.append("| " + " | ".join(row) + " |")

    return "\n".join(md_lines)


def sort_blocks_by_column(blocks: list[tuple]) -> tuple[list[tuple], bool]:
    """Sort PyMuPDF text blocks to preserve multi-column reading order.
    
    Args:
        blocks: List of tuples (x0, y0, x1, y1, text, block_no, block_type)
        
    Returns:
        (sorted_blocks, has_multicolumn_flag)
    """
    text_blocks = [b for b in blocks if b[6] == 0 and b[4].strip()]
    if len(text_blocks) < 2:
        return blocks, False

    # Check for 2-column layout:
    # A 2-column layout has blocks whose x0 and x1 cluster into distinct horizontal zones
    # with a clear vertical gutter between them.
    page_left = min(b[0] for b in text_blocks)
    page_right = max(b[2] for b in text_blocks)
    page_width = page_right - page_left

    if page_width < 100:
        return blocks, False

    midpoint = page_left + page_width / 2.0

    left_blocks = [b for b in text_blocks if b[2] <= midpoint + 15]
    right_blocks = [b for b in text_blocks if b[0] >= midpoint - 15]

    # If significant number of blocks fall into left and right with minimal overlap
    is_multi_column = (
        len(left_blocks) >= 2
        and len(right_blocks) >= 2
        and (len(left_blocks) + len(right_blocks)) >= len(text_blocks) * 0.8
    )

    if not is_multi_column:
        # Standard top-to-bottom sort
        return sorted(text_blocks, key=lambda b: (round(b[1] / 10) * 10, b[0])), False

    # Sort left column top-to-bottom, then right column top-to-bottom
    sorted_left = sorted(left_blocks, key=lambda b: b[1])
    sorted_right = sorted(right_blocks, key=lambda b: b[1])

    # Any spanning blocks (e.g. top title spanning full width)
    spanning = [b for b in text_blocks if b not in left_blocks and b not in right_blocks]
    sorted_all = []
    
    # Place top spanning blocks before columns
    top_spanning = [b for b in spanning if b[1] < min(min(b[1] for b in left_blocks), min(b[1] for b in right_blocks))]
    bottom_spanning = [b for b in spanning if b not in top_spanning]

    sorted_all.extend(sorted(top_spanning, key=lambda b: b[1]))
    sorted_all.extend(sorted_left)
    sorted_all.extend(sorted_right)
    sorted_all.extend(sorted(bottom_spanning, key=lambda b: b[1]))

    return sorted_all, True


def extract_tables_from_page(page) -> list[dict[str, Any]]:
    """Extract tables from a PyMuPDF page using find_tables()."""
    tables = []
    try:
        table_finder = page.find_tables()
        if table_finder and table_finder.tables:
            for tab in table_finder.tables:
                extracted = tab.extract()
                if extracted and len(extracted) > 1:
                    md_table = format_table_as_markdown(extracted)
                    tables.append({
                        "bbox": tab.bbox,
                        "row_count": len(extracted),
                        "col_count": len(extracted[0]),
                        "markdown": md_table,
                        "raw_data": extracted,
                    })
    except Exception:
        # If table extraction fails on this page, gracefully proceed
        pass
    return tables


def clean_page_text(
    page_number: int,
    raw_text: str,
    page_obj=None,
    detected_headers: Optional[set[str]] = None,
    detected_footers: Optional[set[str]] = None,
) -> CleanedPageResult:
    """Perform deterministic text cleaning and normalization for a single page.
    
    Args:
        page_number: 1-indexed page number.
        raw_text: Untouched text extracted from the page.
        page_obj: Optional PyMuPDF page object for table & column extraction.
        detected_headers: Set of repeated header line signatures.
        detected_footers: Set of repeated footer line signatures.
        
    Returns:
        CleanedPageResult with cleaned text, raw text, and tracing metadata.
    """
    if not raw_text or not raw_text.strip():
        return CleanedPageResult(
            page_number=page_number,
            raw_text=raw_text or "",
            cleaned_text="",
            char_count=0,
            has_table=False,
            table_count=0,
            has_multicolumn=False,
            line_fixes_count=0,
            removed_header_footer_count=0,
            cleaning_metadata={"empty": True},
        )

    has_table = False
    table_count = 0
    tables = []
    has_multicolumn = False

    # 1. Multi-column and Table extraction from page object if available
    extracted_text = raw_text
    if page_obj is not None:
        try:
            # Extract tables
            tables = extract_tables_from_page(page_obj)
            if tables:
                has_table = True
                table_count = len(tables)

            # Check multi-column layout
            blocks = page_obj.get_text("blocks")
            sorted_blocks, is_multi = sort_blocks_by_column(blocks)
            if is_multi:
                has_multicolumn = True
                extracted_text = "\n\n".join(b[4].strip() for b in sorted_blocks if b[4].strip())
        except Exception:
            extracted_text = raw_text

    # 2. Whitespace and artefact normalization
    normalized = normalize_whitespace_and_artefacts(extracted_text)
    lines = normalized.split("\n")

    # 3. Repeated header/footer and page-number noise removal
    cleaned_lines: list[str] = []
    removed_header_footer_count = 0
    total_lines = len(lines)

    for idx, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            cleaned_lines.append("")
            continue

        # Check page number noise
        if is_page_number_noise(line_stripped):
            # Only treat as noise if it's near top (first 4 lines) or bottom (last 4 lines)
            if idx < 4 or idx >= total_lines - 4:
                removed_header_footer_count += 1
                continue

        # Check repeated headers
        if detected_headers and idx < 4 and _matches_detected_header_footer(line_stripped, detected_headers):
            removed_header_footer_count += 1
            continue

        # Check repeated footers
        if detected_footers and idx >= total_lines - 4 and _matches_detected_header_footer(line_stripped, detected_footers):
            removed_header_footer_count += 1
            continue

        cleaned_lines.append(line_stripped)

    # 4. Broken line-break and hyphenation correction
    text_with_fixes, line_fixes = fix_broken_linebreaks_and_hyphens(cleaned_lines)

    # 5. Append extracted markdown tables if not already embedded
    if tables:
        table_markdowns = [t["markdown"] for t in tables if t.get("markdown")]
        if table_markdowns:
            combined_tables = "\n\n".join(table_markdowns)
            # If the tables contain text not already in the main body, append them clearly
            if not any(t["markdown"][:30] in text_with_fixes for t in tables if len(t.get("markdown", "")) >= 30):
                text_with_fixes = f"{text_with_fixes}\n\n{combined_tables}".strip()

    final_text = text_with_fixes.strip()

    return CleanedPageResult(
        page_number=page_number,
        raw_text=raw_text,
        cleaned_text=final_text,
        char_count=len(final_text),
        has_table=has_table,
        table_count=table_count,
        has_multicolumn=has_multicolumn,
        line_fixes_count=line_fixes,
        removed_header_footer_count=removed_header_footer_count,
        cleaning_metadata={
            "raw_char_count": len(raw_text),
            "cleaned_char_count": len(final_text),
            "line_fixes": line_fixes,
            "removed_headers_footers": removed_header_footer_count,
            "tables_found": table_count,
            "multi_column_detected": has_multicolumn,
        },
    )
