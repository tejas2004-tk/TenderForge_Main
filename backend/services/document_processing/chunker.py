"""TenderForge - Document Processing: Intelligent Paragraph & Sentence Aware Chunker

Features:
- Configurable chunk size and overlap.
- Paragraph-aware and sentence-aware boundary splitting.
- Preserves clauses across page boundaries with explicit relationships.
- Stable deterministic chunk ordering and indexing.
- Page-level provenance and exact character offsets.
- Duplicate chunk detection using normalized content hashing.
- Backward compatible with existing chunk schema (chunk_index, page_number, original_page_numbers, text).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional

DEFAULT_CHUNK_SIZE = 2000
DEFAULT_CHUNK_OVERLAP = 200

# Regex to detect clause and section numbering
_CLAUSE_PATTERN = re.compile(
    r"(?i)\b(?:clause|section|para|paragraph|article|item)\s*(\d+(?:\.\d+)*)\b"
    r"|^(\d+(?:\.\d+)+)\s+[A-Z]"
)

# Regex to split text into sentences
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'‘“(\[])")


@dataclass
class DocumentChunk:
    chunk_index: int
    page_number: int
    original_page_numbers: list[int]
    text: str
    char_start: int
    char_end: int
    clause_id: Optional[str] = None
    is_continuation: bool = False
    continuation_of_chunk_index: Optional[int] = None
    is_duplicate: bool = False
    duplicate_of_chunk_index: Optional[int] = None
    content_hash: str = ""
    estimated_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Conservative token estimation for text chunk (approx 3.5 chars / token)."""
    if not text:
        return 0
    # Two bytes per token gives a safe ceiling for multilingual and technical symbols
    byte_count = len(text.encode("utf-8"))
    return (byte_count + 3) // 3 + 10


def _normalize_for_duplicate_check(text: str) -> str:
    """Normalize text to catch exact and near-exact duplicate boilerplate."""
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    return " ".join(tokens)


def _detect_clause_header(text: str) -> Optional[str]:
    """Detect clause identifier from the start of a paragraph or text segment."""
    for line in text.split("\n")[:3]:
        line_clean = line.strip()
        match = _CLAUSE_PATTERN.search(line_clean)
        if match:
            clause_num = match.group(1) or match.group(2)
            if clause_num:
                return f"clause_{clause_num}"
        # Heading pattern like "SECTION 3: ELIGIBILITY"
        heading_match = re.match(r"^(SECTION|PART|CHAPTER|ANNEXURE|APPENDIX)\s+([A-Z0-9.-]+)", line_clean, re.I)
        if heading_match:
            return f"{heading_match.group(1).lower()}_{heading_match.group(2).lower()}"
    return None


def split_paragraph_into_sentences(para: str, max_chunk_size: int) -> list[str]:
    """Split a large paragraph into sentence-aware units that fit within max_chunk_size."""
    if len(para) <= max_chunk_size:
        return [para]

    sentences = _SENTENCE_SPLIT_RE.split(para)
    units: list[str] = []
    current_unit: list[str] = []
    current_len = 0

    for sent in sentences:
        sent_len = len(sent)
        if current_len + sent_len + 1 <= max_chunk_size:
            current_unit.append(sent)
            current_len += sent_len + 1
        else:
            if current_unit:
                units.append(" ".join(current_unit))
                current_unit = []
                current_len = 0

            # If an individual sentence is longer than max_chunk_size, split by clause or words
            if sent_len > max_chunk_size:
                clause_parts = re.split(r"(?<=[,;:])\s+", sent)
                c_unit: list[str] = []
                c_len = 0
                for part in clause_parts:
                    if c_len + len(part) + 1 <= max_chunk_size:
                        c_unit.append(part)
                        c_len += len(part) + 1
                    else:
                        if c_unit:
                            units.append(" ".join(c_unit))
                            c_unit = []
                            c_len = 0
                        if len(part) > max_chunk_size:
                            # Hard split on whitespace
                            words = part.split(" ")
                            w_unit: list[str] = []
                            w_len = 0
                            for w in words:
                                if w_len + len(w) + 1 <= max_chunk_size:
                                    w_unit.append(w)
                                    w_len += len(w) + 1
                                else:
                                    if w_unit:
                                        units.append(" ".join(w_unit))
                                    w_unit = [w]
                                    w_len = len(w)
                            if w_unit:
                                units.append(" ".join(w_unit))
                        else:
                            c_unit.append(part)
                            c_len += len(part) + 1
                if c_unit:
                    units.append(" ".join(c_unit))
            else:
                current_unit.append(sent)
                current_len = sent_len

    if current_unit:
        units.append(" ".join(current_unit))

    return units


def create_chunks_for_pages(
    cleaned_pages: list[dict[str, Any]],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    start_chunk_index: int = 0,
) -> list[DocumentChunk]:
    """Create paragraph and sentence aware chunks across all cleaned pages.
    
    Args:
        cleaned_pages: List of dicts with 'page_number' and 'cleaned_text'.
        chunk_size: Maximum characters per chunk.
        chunk_overlap: Desired overlap in characters between chunks.
        start_chunk_index: Starting integer index for chunks.
        
    Returns:
        List of DocumentChunk objects with stable ordering, provenance, and offsets.
    """
    if chunk_size < 100:
        raise ValueError("chunk_size must be at least 100 characters.")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size.")

    chunks: list[DocumentChunk] = []
    seen_hashes: dict[str, int] = {}  # hash -> chunk_index
    current_chunk_idx = start_chunk_index
    active_clause_id: Optional[str] = None
    prev_chunk_in_clause: Optional[int] = None

    for page_dict in cleaned_pages:
        page_number = page_dict["page_number"]
        page_text = page_dict.get("cleaned_text", "").strip()

        if not page_text:
            continue

        # Split page into paragraphs
        raw_paras = re.split(r"\n\s*\n", page_text)
        paras = []
        for p in raw_paras:
            p_strip = p.strip()
            if not p_strip:
                continue
            # If paragraph is too large, split into sentence units
            if len(p_strip) > chunk_size:
                paras.extend(split_paragraph_into_sentences(p_strip, chunk_size))
            else:
                paras.append(p_strip)

        if not paras:
            continue

        # Accumulate paragraphs into chunks
        current_paras: list[str] = []
        current_len = 0
        para_start_char = 0

        for para in paras:
            para_len = len(para)
            clause_found = _detect_clause_header(para)
            if clause_found:
                active_clause_id = clause_found
                prev_chunk_in_clause = None

            # Would adding this paragraph exceed chunk_size?
            if current_len + para_len + 2 > chunk_size and current_paras:
                # Flush current chunk
                chunk_text = "\n\n".join(current_paras)
                char_end = para_start_char

                norm_hash = hashlib.sha256(_normalize_for_duplicate_check(chunk_text).encode()).hexdigest()
                is_duplicate = norm_hash in seen_hashes
                dup_of = seen_hashes.get(norm_hash)
                if not is_duplicate:
                    seen_hashes[norm_hash] = current_chunk_idx

                is_continuation = bool(prev_chunk_in_clause is not None and active_clause_id)

                chunk = DocumentChunk(
                    chunk_index=current_chunk_idx,
                    page_number=page_number,
                    original_page_numbers=[page_number],
                    text=chunk_text,
                    char_start=max(0, char_end - len(chunk_text)),
                    char_end=char_end,
                    clause_id=active_clause_id,
                    is_continuation=is_continuation,
                    continuation_of_chunk_index=prev_chunk_in_clause if is_continuation else None,
                    is_duplicate=is_duplicate,
                    duplicate_of_chunk_index=dup_of,
                    content_hash=norm_hash,
                    estimated_tokens=estimate_tokens(chunk_text),
                )
                chunks.append(chunk)
                prev_chunk_in_clause = current_chunk_idx
                current_chunk_idx += 1

                # Overlap handling: retain last paragraph if it fits within overlap limit
                if chunk_overlap > 0 and len(current_paras[-1]) <= chunk_overlap:
                    overlap_para = current_paras[-1]
                    current_paras = [overlap_para, para]
                    current_len = len(overlap_para) + 2 + para_len
                else:
                    current_paras = [para]
                    current_len = para_len
            else:
                current_paras.append(para)
                current_len += para_len + 2

            para_start_char = page_text.find(para, para_start_char) + para_len

        # Flush remaining paragraphs on this page
        if current_paras:
            chunk_text = "\n\n".join(current_paras)
            norm_hash = hashlib.sha256(_normalize_for_duplicate_check(chunk_text).encode()).hexdigest()
            is_duplicate = norm_hash in seen_hashes
            dup_of = seen_hashes.get(norm_hash)
            if not is_duplicate:
                seen_hashes[norm_hash] = current_chunk_idx

            is_continuation = bool(prev_chunk_in_clause is not None and active_clause_id)

            char_start = page_text.rfind(chunk_text)
            if char_start < 0:
                char_start = max(0, len(page_text) - len(chunk_text))
            char_end = char_start + len(chunk_text)

            chunk = DocumentChunk(
                chunk_index=current_chunk_idx,
                page_number=page_number,
                original_page_numbers=[page_number],
                text=chunk_text,
                char_start=char_start,
                char_end=char_end,
                clause_id=active_clause_id,
                is_continuation=is_continuation,
                continuation_of_chunk_index=prev_chunk_in_clause if is_continuation else None,
                is_duplicate=is_duplicate,
                duplicate_of_chunk_index=dup_of,
                content_hash=norm_hash,
                estimated_tokens=estimate_tokens(chunk_text),
            )
            chunks.append(chunk)
            prev_chunk_in_clause = current_chunk_idx
            current_chunk_idx += 1

    return chunks

