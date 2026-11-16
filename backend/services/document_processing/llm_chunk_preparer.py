"""TenderForge - Document Processing: LLM-Ready Chunk Preparation

Prepares high-quality, curated, token-budgeted chunks for future LLM analysis:
- Selects high-relevance chunks deterministically.
- Enriches chunks with surrounding context where needed.
- Deduplicates boilerplate and repeated sections.
- Preserves exact page provenance, character offsets, and cross-page clause relationships.
- Supports configurable token budgets and prioritizes critical requirements.
- Retains unclassified / uncertain chunks in a secondary reserve pool.
- Enables instant source text retrieval for every chunk.
- Pure Python: does not invoke any LLM or external services.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional


@dataclass
class LLMReadyChunk:
    chunk_index: int
    page_number: int
    original_page_numbers: list[int]
    text: str
    char_start: int
    char_end: int
    relevance_category: str
    relevance_score: int
    matched_keywords: list[str]
    selection_reason: str
    estimated_tokens: int
    clause_id: Optional[str] = None
    is_continuation: bool = False
    surrounding_context: str = ""
    is_reserve: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Safe token estimation (approx 2 UTF-8 bytes per token + headroom)."""
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / 2.5) + 10


def prepare_llm_ready_chunks(
    chunks: list[Any],
    classifications: list[Any],
    max_token_budget: int = 6000,
    min_score_threshold: int = 15,
) -> tuple[list[LLMReadyChunk], list[LLMReadyChunk], dict[str, Any]]:
    """Curate and prepare chunks for LLM consumption within a configurable token budget.
    
    Args:
        chunks: List of DocumentChunk or chunk dicts.
        classifications: List of ClassificationResult or classification dicts.
        max_token_budget: Target maximum token budget for the candidate pool.
        min_score_threshold: Minimum relevance score to qualify as primary candidate.
        
    Returns:
        (selected_candidate_chunks, reserve_chunks, preparation_summary)
    """
    if len(chunks) != len(classifications):
        raise ValueError("Chunks and classifications lists must have identical lengths.")

    primary_candidates: list[dict[str, Any]] = []
    reserve_candidates: list[dict[str, Any]] = []

    seen_fingerprints = set()

    for i in range(len(chunks)):
        c = chunks[i]
        cls = classifications[i]

        text = c.text if hasattr(c, "text") else c.get("text", "")
        page_num = c.page_number if hasattr(c, "page_number") else c.get("page_number", 1)
        orig_pages = c.original_page_numbers if hasattr(c, "original_page_numbers") else c.get("original_page_numbers", [page_num])
        c_idx = c.chunk_index if hasattr(c, "chunk_index") else c.get("chunk_index", i)
        char_start = c.char_start if hasattr(c, "char_start") else c.get("char_start", 0)
        char_end = c.char_end if hasattr(c, "char_end") else c.get("char_end", len(text))
        clause_id = c.clause_id if hasattr(c, "clause_id") else c.get("clause_id")
        is_cont = c.is_continuation if hasattr(c, "is_continuation") else c.get("is_continuation", False)
        is_dup = c.is_duplicate if hasattr(c, "is_duplicate") else c.get("is_duplicate", False)

        rel_cat = cls.relevance_category if hasattr(cls, "relevance_category") else cls.get("relevance_category", "Unclassified")
        rel_score = cls.relevance_score if hasattr(cls, "relevance_score") else cls.get("relevance_score", 0)
        matched_kw = cls.matched_keywords if hasattr(cls, "matched_keywords") else cls.get("matched_keywords", [])
        sel_reason = cls.selection_reason if hasattr(cls, "selection_reason") else cls.get("selection_reason", "")
        is_cand = cls.is_llm_candidate if hasattr(cls, "is_llm_candidate") else cls.get("is_llm_candidate", False)

        toks = estimate_tokens(text)

        # Context detection (e.g. preceding chunk summary or heading)
        surrounding_context = ""
        if is_cont and i > 0:
            prev_chunk = chunks[i - 1]
            prev_text = prev_chunk.text if hasattr(prev_chunk, "text") else prev_chunk.get("text", "")
            surrounding_context = f"[Continuation of clause '{clause_id or 'preceding'}' from page {prev_chunk.page_number if hasattr(prev_chunk, 'page_number') else prev_chunk.get('page_number')}]"

        item = {
            "chunk_index": c_idx,
            "page_number": page_num,
            "original_page_numbers": orig_pages,
            "text": text,
            "char_start": char_start,
            "char_end": char_end,
            "relevance_category": rel_cat,
            "relevance_score": rel_score,
            "matched_keywords": matched_kw,
            "selection_reason": sel_reason,
            "estimated_tokens": toks,
            "clause_id": clause_id,
            "is_continuation": is_cont,
            "surrounding_context": surrounding_context,
            "is_duplicate": is_dup,
        }

        # Deduplicate exact duplicate items from primary pool
        content_key = (rel_cat, text[:100].strip().casefold())
        if is_dup or content_key in seen_fingerprints:
            item["is_reserve"] = True
            item["selection_reason"] += " (Deduplicated from primary pool)"
            reserve_candidates.append(item)
            continue

        if is_cand and rel_score >= min_score_threshold:
            seen_fingerprints.add(content_key)
            primary_candidates.append(item)
        else:
            reserve_candidates.append(item)

    # Sort primary candidates by relevance score descending to prioritize most critical requirements
    # while tracking category representation
    sorted_by_priority = sorted(
        primary_candidates,
        key=lambda x: (x["relevance_score"], -x["chunk_index"]),
        reverse=True,
    )

    selected_chunks: list[LLMReadyChunk] = []
    accumulated_tokens = 0

    for item in sorted_by_priority:
        item_tokens = item["estimated_tokens"]
        if accumulated_tokens + item_tokens <= max_token_budget or not selected_chunks:
            # Include in primary candidate set
            ready_obj = LLMReadyChunk(
                chunk_index=item["chunk_index"],
                page_number=item["page_number"],
                original_page_numbers=item["original_page_numbers"],
                text=item["text"],
                char_start=item["char_start"],
                char_end=item["char_end"],
                relevance_category=item["relevance_category"],
                relevance_score=item["relevance_score"],
                matched_keywords=item["matched_keywords"],
                selection_reason=item["selection_reason"],
                estimated_tokens=item_tokens,
                clause_id=item["clause_id"],
                is_continuation=item["is_continuation"],
                surrounding_context=item["surrounding_context"],
                is_reserve=False,
            )
            selected_chunks.append(ready_obj)
            accumulated_tokens += item_tokens
        else:
            # Fits in reserve
            item["is_reserve"] = True
            item["selection_reason"] += " (Exceeded primary token budget, retained in reserve)"
            reserve_candidates.append(item)

    # Sort selected primary chunks back into natural document order (by chunk_index)
    selected_chunks.sort(key=lambda x: x.chunk_index)

    # Format reserve pool as LLMReadyChunk objects
    reserve_chunks: list[LLMReadyChunk] = [
        LLMReadyChunk(
            chunk_index=r["chunk_index"],
            page_number=r["page_number"],
            original_page_numbers=r["original_page_numbers"],
            text=r["text"],
            char_start=r["char_start"],
            char_end=r["char_end"],
            relevance_category=r["relevance_category"],
            relevance_score=r["relevance_score"],
            matched_keywords=r["matched_keywords"],
            selection_reason=r["selection_reason"],
            estimated_tokens=r["estimated_tokens"],
            clause_id=r["clause_id"],
            is_continuation=r["is_continuation"],
            surrounding_context=r.get("surrounding_context", ""),
            is_reserve=True,
        )
        for r in sorted(reserve_candidates, key=lambda x: x["chunk_index"])
    ]

    summary = {
        "total_source_chunks": len(chunks),
        "selected_candidate_chunks": len(selected_chunks),
        "reserve_chunks": len(reserve_chunks),
        "total_selected_tokens": accumulated_tokens,
        "max_token_budget": max_token_budget,
        "budget_utilized_percentage": round(min(100.0, (accumulated_tokens / max(1, max_token_budget)) * 100), 1),
        "category_distribution": {
            cat: sum(1 for c in selected_chunks if c.relevance_category == cat)
            for cat in set(c.relevance_category for c in selected_chunks)
        },
    }

    return selected_chunks, reserve_chunks, summary


def retrieve_source_text_for_chunk(
    chunk: LLMReadyChunk,
    pages_collection: Any,
    user_id: Any,
    tender_id: Any,
    file_hash: str,
) -> str:
    """Retrieve the original extracted raw page text for a given chunk."""
    page_rows = list(pages_collection.find({
        "user_id": user_id,
        "tender_id": tender_id,
        "file_hash": file_hash,
        "page_number": chunk.page_number,
    }).sort("part_index", 1))

    if not page_rows:
        return ""
    return "\n".join(row.get("text", "") for row in page_rows)

