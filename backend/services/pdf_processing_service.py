import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional
from uuid import uuid4

import fitz
from flask import current_app
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from database import get_database
from services.document_processing import (
    PDFValidationError,
    assess_document_ocr_status,
    clean_page_text,
    classify_document_chunks,
    create_chunks_for_pages,
    detect_repeated_headers_and_footers,
    inspect_pdf_page,
    open_and_validate_pdf,
    prepare_llm_ready_chunks,
)
from services.tender_service import TenderError, _find_owned_tender, _tender_directory

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200
PAGE_TEXT_PART_SIZE = 100_000
WRITE_BATCH_PAGE_COUNT = 5
WRITE_BATCH_BYTE_LIMIT = 1_000_000
LOW_TEXT_CHARACTER_LIMIT = 40
PROCESSING_LEASE = timedelta(minutes=30)
_INDEX_LOCK = Lock()
_INDEXES_READY = False
_logger = logging.getLogger(__name__)


class ExtractionError(TenderError):
    pass


def _collections():
    cols = _all_collections()
    return cols[0], cols[1], cols[2]


def _all_collections():
    global _INDEXES_READY
    database = get_database(current_app)
    if not _INDEXES_READY:
        with _INDEX_LOCK:
            if not _INDEXES_READY:
                database.tender_extractions.create_index(
                    [("user_id", ASCENDING), ("tender_id", ASCENDING)],
                    unique=True,
                    name="unique_extraction_per_user_tender",
                )
                database.tender_extracted_pages.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("tender_id", ASCENDING),
                        ("file_hash", ASCENDING),
                        ("page_number", ASCENDING),
                        ("part_index", ASCENDING),
                    ],
                    unique=True,
                    name="unique_extracted_page_part",
                )
                database.tender_extracted_chunks.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("tender_id", ASCENDING),
                        ("file_hash", ASCENDING),
                        ("chunk_index", ASCENDING),
                    ],
                    unique=True,
                    name="unique_extracted_chunk",
                )
                database.tender_extracted_chunks.create_index(
                    [("user_id", ASCENDING), ("tender_id", ASCENDING), ("file_hash", ASCENDING)],
                    name="chunks_by_tender_version",
                )
                database.tender_cleaned_pages.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("tender_id", ASCENDING),
                        ("file_hash", ASCENDING),
                        ("page_number", ASCENDING),
                    ],
                    unique=True,
                    name="unique_cleaned_page_part",
                )
                database.tender_llm_ready_chunks.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("tender_id", ASCENDING),
                        ("file_hash", ASCENDING),
                        ("chunk_index", ASCENDING),
                    ],
                    unique=True,
                    name="unique_llm_ready_chunk",
                )
                database.tender_llm_ready_chunks.create_index(
                    [
                        ("user_id", ASCENDING),
                        ("tender_id", ASCENDING),
                        ("file_hash", ASCENDING),
                        ("relevance_score", DESCENDING),
                    ],
                    name="llm_ready_chunks_by_relevance",
                )
                _INDEXES_READY = True
    return (
        database.tender_extractions,
        database.tender_extracted_pages,
        database.tender_extracted_chunks,
        database.tender_cleaned_pages,
        database.tender_llm_ready_chunks,
    )


def split_page_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split a page into overlapping chunks without dropping characters (backward compatibility)."""
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Chunk size must be positive and overlap must be smaller than the chunk size.")
    return list(_iter_page_chunks(text, chunk_size, overlap))


def _iter_page_chunks(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Chunk size must be positive and overlap must be smaller than the chunk size.")
    if not text:
        return
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def _iter_page_storage_text(text):
    if not text:
        yield ""
        return
    for start in range(0, len(text), PAGE_TEXT_PART_SIZE):
        yield text[start:start + PAGE_TEXT_PART_SIZE]


def _file_sha256(file_path):
    digest = hashlib.sha256()
    with file_path.open("rb") as pdf_file:
        for block in iter(lambda: pdf_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _summary(metadata, reused=False, in_progress=False):
    if not metadata:
        return {
            "status": "not_processed",
            "page_count": 0,
            "extracted_pages": 0,
            "total_chunks": 0,
            "processing_started_at": None,
            "processing_finished_at": None,
            "error_message": None,
            "reused": reused,
            "in_progress": in_progress,
            "total_cleaned_pages": 0,
            "total_llm_ready_chunks": 0,
        }

    return {
        "status": metadata.get("status", "not_processed"),
        "page_count": metadata.get("page_count", 0),
        "extracted_pages": metadata.get("extracted_pages", 0),
        "total_chunks": metadata.get("total_chunks", 0),
        "processing_started_at": _iso_date(metadata.get("processing_started_at")),
        "processing_finished_at": _iso_date(metadata.get("processing_finished_at")),
        "error_message": metadata.get("error_message"),
        "reused": reused,
        "in_progress": in_progress,
        "total_cleaned_pages": metadata.get("total_cleaned_pages", 0),
        "total_llm_ready_chunks": metadata.get("total_llm_ready_chunks", 0),
    }


def _iso_date(value):
    return value.isoformat() if value else None


def _as_utc(value):
    if value and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _counts(user_id, tender_id, file_hash, pages, chunks):
    query = {
        "user_id": user_id,
        "tender_id": tender_id,
        "file_hash": file_hash,
        "part_index": 0,
        "complete": True,
    }
    extracted_pages = pages.count_documents(query)
    total_chunks = chunks.count_documents({
        "user_id": user_id,
        "tender_id": tender_id,
        "file_hash": file_hash,
    })
    return extracted_pages, total_chunks


def _log_timings(status, page_count, total_chunks, timings_ms):
    if current_app.config.get("DEBUG"):
        _logger.info(
            "PDF processing status=%s pages=%d chunks=%d sha256_ms=%.1f "
            "pdf_text_extraction_ms=%.1f text_cleaning_ms=%.1f chunk_creation_ms=%.1f "
            "relevance_classification_ms=%.1f llm_prep_ms=%.1f "
            "mongo_page_writes_ms=%.1f mongo_chunk_writes_ms=%.1f total_ms=%.1f",
            status,
            page_count,
            total_chunks,
            timings_ms.get("sha256_calculation_ms", 0.0),
            timings_ms.get("pdf_text_extraction_ms", 0.0),
            timings_ms.get("text_cleaning_ms", 0.0),
            timings_ms.get("chunk_creation_ms", 0.0),
            timings_ms.get("relevance_classification_ms", 0.0),
            timings_ms.get("llm_prep_ms", 0.0),
            timings_ms.get("mongodb_page_writes_ms", 0.0),
            timings_ms.get("mongodb_chunk_writes_ms", 0.0),
            timings_ms.get("total_processing_ms", 0.0),
        )


def _mark_failed(
    metadata_collection,
    pages,
    chunks,
    user_id,
    tender_id,
    file_hash,
    token,
    message,
    page_count,
    timings_ms,
    processing_started,
):
    now = datetime.now(timezone.utc)
    extracted_pages, total_chunks = _counts(user_id, tender_id, file_hash, pages, chunks)
    timings_ms["total_processing_ms"] = (time.perf_counter() - processing_started) * 1000
    metadata_collection.update_one(
        {
            "user_id": user_id,
            "tender_id": tender_id,
            "file_hash": file_hash,
            "processing_token": token,
        },
        {
            "$set": {
                "status": "failed",
                "page_count": page_count,
                "extracted_pages": extracted_pages,
                "total_chunks": total_chunks,
                "error_message": message,
                "processing_finished_at": now,
                "updated_at": now,
                "lease_until": None,
                "timings_ms": dict(timings_ms),
            },
            "$unset": {"processing_token": ""},
        },
    )
    current = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_id})
    _log_timings("failed", page_count, total_chunks, timings_ms)
    return _summary(current)


def _acquire_processing_claim(metadata_collection, user_id, tender_id, file_hash):
    now = datetime.now(timezone.utc)
    existing = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_id})
    if existing and existing.get("file_hash") == file_hash:
        if existing.get("status") in {"completed", "needs_ocr"}:
            return existing, None, "reused"
        lease_until = _as_utc(existing.get("lease_until"))
        if existing.get("status") == "processing" and lease_until and lease_until > now:
            return existing, None, "in_progress"

    token = uuid4().hex
    next_attempt = (existing.get("attempt_count", 0) if existing else 0) + 1
    new_version = bool(existing and existing.get("file_hash") != file_hash)
    set_values = {
        "file_hash": file_hash,
        "status": "processing",
        "processing_token": token,
        "processing_started_at": now,
        "processing_finished_at": None,
        "updated_at": now,
        "lease_until": now + PROCESSING_LEASE,
        "error_message": None,
        "attempt_count": next_attempt,
    }
    if new_version or not existing:
        set_values.update({
            "page_count": 0,
            "extracted_pages": 0,
            "total_chunks": 0,
            "total_cleaned_pages": 0,
            "total_llm_ready_chunks": 0,
        })

    try:
        claimed = metadata_collection.find_one_and_update(
            {
                "user_id": user_id,
                "tender_id": tender_id,
                "$or": [
                    {"status": {"$ne": "processing"}},
                    {"lease_until": {"$lte": now}},
                    {"lease_until": {"$exists": False}},
                    {"file_hash": {"$ne": file_hash}},
                ],
            },
            {
                "$set": set_values,
                "$setOnInsert": {"user_id": user_id, "tender_id": tender_id, "created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        claimed = None

    if claimed:
        return claimed, token, "claimed"

    current = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_id})
    if current and current.get("file_hash") == file_hash and current.get("status") in {"completed", "needs_ocr"}:
        return current, None, "reused"
    return current, None, "in_progress"


def process_tender(user, tender_id):
    """Execute complete Phase 2 document processing pipeline.
    
    1. Safe PDF opening & checksum verification.
    2. Page-by-page extraction of raw text with image / OCR detection.
    3. Repeated header/footer detection across document.
    4. Deterministic text cleaning, broken line-break fixes, table & column extraction.
    5. Paragraph and sentence aware chunking with clause provenance.
    6. Python-based relevance classification across 20+ topics with context boosting.
    7. LLM-ready candidate chunk preparation within token budget.
    8. Batched database persistence into MongoDB with safe lease management.
    """
    processing_started = time.perf_counter()
    timings_ms = {
        "sha256_calculation_ms": 0.0,
        "pdf_text_extraction_ms": 0.0,
        "scanned_pdf_detection_ms": 0.0,
        "text_cleaning_ms": 0.0,
        "chunk_creation_ms": 0.0,
        "relevance_classification_ms": 0.0,
        "llm_prep_ms": 0.0,
        "mongodb_page_writes_ms": 0.0,
        "mongodb_chunk_writes_ms": 0.0,
    }
    tender = _find_owned_tender(user, tender_id)
    file_path = _tender_directory() / tender["storage_name"]
    if not file_path.is_file():
        raise ExtractionError("Tender PDF file could not be found.", 404)

    started = time.perf_counter()
    file_hash = _file_sha256(file_path)
    timings_ms["sha256_calculation_ms"] = (time.perf_counter() - started) * 1000
    if tender.get("sha256") and file_hash != tender["sha256"]:
        raise ExtractionError("The stored PDF no longer matches its upload checksum.", 409)

    user_id = user["_id"]
    tender_object_id = tender["_id"]
    metadata_collection, pages, chunks, cleaned_pages_col, llm_ready_col = _all_collections()
    existing = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
    previous_hash = existing.get("file_hash") if existing else None
    metadata, token, claim_status = _acquire_processing_claim(
        metadata_collection, user_id, tender_object_id, file_hash
    )
    if claim_status == "reused":
        return _summary(metadata, reused=True)
    if claim_status == "in_progress":
        return _summary(metadata, in_progress=True)

    version_query = {"user_id": user_id, "tender_id": tender_object_id, "file_hash": file_hash}
    if previous_hash and previous_hash != file_hash:
        pages.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})
        chunks.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})
        cleaned_pages_col.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})
        llm_ready_col.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})

    current_page_count = 0
    try:
        # Clear partial records for this hash before starting clean pipeline
        pages.delete_many(version_query)
        chunks.delete_many(version_query)
        cleaned_pages_col.delete_many(version_query)
        llm_ready_col.delete_many(version_query)

        # 1. Safe opening & validation
        try:
            document = open_and_validate_pdf(file_path)
        except PDFValidationError as val_err:
            return _mark_failed(
                metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                val_err.message, 0, timings_ms, processing_started,
            )
        except Exception:
            return _mark_failed(
                metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                "Could not read this PDF. It may be corrupted or unsupported.", 0,
                timings_ms, processing_started,
            )

        with document:
            current_page_count = document.page_count
            metadata_collection.update_one(
                {"user_id": user_id, "tender_id": tender_object_id, "processing_token": token},
                {"$set": {"page_count": current_page_count, "updated_at": datetime.now(timezone.utc)}},
            )

            # 2. Page-by-page raw extraction and OCR / image inspection
            raw_page_texts = []
            page_infos = []
            page_docs = []

            for page_index in range(current_page_count):
                page_number = page_index + 1
                try:
                    page = document.load_page(page_index)
                    stage_started = time.perf_counter()
                    raw_text, info = inspect_pdf_page(page, page_number)
                    timings_ms["pdf_text_extraction_ms"] += (time.perf_counter() - stage_started) * 1000
                    raw_page_texts.append(raw_text)
                    page_infos.append(info)
                except Exception:
                    return _mark_failed(
                        metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                        f"Text extraction failed on page {page_number}. You can retry processing.",
                        current_page_count, timings_ms, processing_started,
                    )

                # Prepare raw page documents for persistence
                character_count = len(raw_text.strip())
                part_count = max(1, (len(raw_text) + PAGE_TEXT_PART_SIZE - 1) // PAGE_TEXT_PART_SIZE)
                for part_index, part in enumerate(_iter_page_storage_text(raw_text)):
                    page_docs.append({
                        "user_id": user_id,
                        "tender_id": tender_object_id,
                        "file_hash": file_hash,
                        "page_number": page_number,
                        "part_index": part_index,
                        "part_count": part_count,
                        "text_character_count": character_count,
                        "has_page_image": info.has_image,
                        "text": part,
                        "complete": True,
                    })

            # 3. Assess overall document OCR status
            ocr_assessment = assess_document_ocr_status(current_page_count, page_infos)
            if ocr_assessment.status in {"needs_ocr", "failed"}:
                now = datetime.now(timezone.utc)
                timings_ms["total_processing_ms"] = (time.perf_counter() - processing_started) * 1000
                metadata_collection.update_one(
                    {
                        "user_id": user_id,
                        "tender_id": tender_object_id,
                        "file_hash": file_hash,
                        "processing_token": token,
                    },
                    {
                        "$set": {
                            "status": ocr_assessment.status,
                            "page_count": current_page_count,
                            "extracted_pages": 0,
                            "total_chunks": 0,
                            "error_message": ocr_assessment.error_message,
                            "processing_finished_at": now,
                            "updated_at": now,
                            "lease_until": None,
                            "timings_ms": dict(timings_ms),
                        },
                        "$unset": {"processing_token": ""},
                    },
                )
                final_metadata = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
                return _summary(final_metadata)

            # 4. Text cleaning and normalization
            cleaning_started = time.perf_counter()
            pages_raw_lines = [t.split("\n") for t in raw_page_texts]
            detected_headers, detected_footers = detect_repeated_headers_and_footers(pages_raw_lines)

            cleaned_page_results = []
            cleaned_page_docs = []

            for page_index in range(current_page_count):
                page_number = page_index + 1
                page = document.load_page(page_index)
                raw_text = raw_page_texts[page_index]
                cleaned_res = clean_page_text(
                    page_number=page_number,
                    raw_text=raw_text,
                    page_obj=page,
                    detected_headers=detected_headers,
                    detected_footers=detected_footers,
                )
                cleaned_page_results.append(cleaned_res)
                cleaned_page_docs.append({
                    "user_id": user_id,
                    "tender_id": tender_object_id,
                    "file_hash": file_hash,
                    "page_number": page_number,
                    "cleaned_text": cleaned_res.cleaned_text,
                    "char_count": cleaned_res.char_count,
                    "has_table": cleaned_res.has_table,
                    "table_count": cleaned_res.table_count,
                    "has_multicolumn": cleaned_res.has_multicolumn,
                    "line_fixes_count": cleaned_res.line_fixes_count,
                    "removed_header_footer_count": cleaned_res.removed_header_footer_count,
                    "cleaning_metadata": cleaned_res.cleaning_metadata,
                })
            timings_ms["text_cleaning_ms"] = (time.perf_counter() - cleaning_started) * 1000

            # 5. Paragraph-aware and sentence-aware chunking
            chunking_started = time.perf_counter()
            cleaned_pages_input = [
                {"page_number": c.page_number, "cleaned_text": c.cleaned_text}
                for c in cleaned_page_results
            ]
            chunks_list = create_chunks_for_pages(
                cleaned_pages_input,
                chunk_size=CHUNK_SIZE,
                chunk_overlap=CHUNK_OVERLAP,
                start_chunk_index=0,
            )
            timings_ms["chunk_creation_ms"] = (time.perf_counter() - chunking_started) * 1000

            # 6. Python-based relevance classification across 20+ topics
            classification_started = time.perf_counter()
            classifications = classify_document_chunks(chunks_list)
            timings_ms["relevance_classification_ms"] = (time.perf_counter() - classification_started) * 1000

            # 7. LLM-ready candidate chunk preparation
            prep_started = time.perf_counter()
            token_budget = int(current_app.config.get("ANALYSIS_MAX_INPUT_TOKENS", 6000))
            selected_llm_chunks, reserve_chunks, prep_summary = prepare_llm_ready_chunks(
                chunks=chunks_list,
                classifications=classifications,
                max_token_budget=token_budget,
            )
            timings_ms["llm_prep_ms"] = (time.perf_counter() - prep_started) * 1000

            # 8. Prepare documents for MongoDB persistence
            chunk_docs = []
            for i, chunk in enumerate(chunks_list):
                cls = classifications[i]
                chunk_docs.append({
                    "user_id": user_id,
                    "tender_id": tender_object_id,
                    "file_hash": file_hash,
                    "chunk_index": chunk.chunk_index,
                    "page_number": chunk.page_number,
                    "original_page_numbers": chunk.original_page_numbers,
                    "text": chunk.text,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "relevance_category": cls.relevance_category,
                    "relevance_categories": cls.relevance_categories,
                    "relevance_score": cls.relevance_score,
                    "matched_keywords": cls.matched_keywords,
                    "selection_reason": cls.selection_reason,
                    "is_llm_candidate": cls.is_llm_candidate,
                    "clause_id": chunk.clause_id,
                    "is_continuation": chunk.is_continuation,
                    "continuation_of_chunk_index": chunk.continuation_of_chunk_index,
                    "is_duplicate": chunk.is_duplicate,
                    "duplicate_of_chunk_index": chunk.duplicate_of_chunk_index,
                    "estimated_tokens": chunk.estimated_tokens,
                })

            llm_ready_docs = []
            for r_chunk in selected_llm_chunks:
                llm_ready_docs.append({
                    "user_id": user_id,
                    "tender_id": tender_object_id,
                    "file_hash": file_hash,
                    "chunk_index": r_chunk.chunk_index,
                    "page_number": r_chunk.page_number,
                    "original_page_numbers": r_chunk.original_page_numbers,
                    "text": r_chunk.text,
                    "char_start": r_chunk.char_start,
                    "char_end": r_chunk.char_end,
                    "relevance_category": r_chunk.relevance_category,
                    "relevance_score": r_chunk.relevance_score,
                    "matched_keywords": r_chunk.matched_keywords,
                    "selection_reason": r_chunk.selection_reason,
                    "estimated_tokens": r_chunk.estimated_tokens,
                    "clause_id": r_chunk.clause_id,
                    "is_continuation": r_chunk.is_continuation,
                    "surrounding_context": r_chunk.surrounding_context,
                    "is_reserve": False,
                })

            # 9. Batched MongoDB Writes with lease renewal
            write_started = time.perf_counter()
            if page_docs:
                pages.insert_many(page_docs, ordered=True)
            if cleaned_page_docs:
                cleaned_pages_col.insert_many(cleaned_page_docs, ordered=True)
            timings_ms["mongodb_page_writes_ms"] += (time.perf_counter() - write_started) * 1000

            write_started = time.perf_counter()
            if chunk_docs:
                chunks.insert_many(chunk_docs, ordered=True)
            if llm_ready_docs:
                llm_ready_col.insert_many(llm_ready_docs, ordered=True)
            timings_ms["mongodb_chunk_writes_ms"] += (time.perf_counter() - write_started) * 1000

        # Complete status update
        now = datetime.now(timezone.utc)
        extracted_pages = current_page_count
        total_chunks = len(chunk_docs)
        timings_ms["total_processing_ms"] = (time.perf_counter() - processing_started) * 1000

        metadata_collection.update_one(
            {
                "user_id": user_id,
                "tender_id": tender_object_id,
                "file_hash": file_hash,
                "processing_token": token,
            },
            {
                "$set": {
                    "status": "completed",
                    "page_count": current_page_count,
                    "extracted_pages": extracted_pages,
                    "total_chunks": total_chunks,
                    "total_cleaned_pages": len(cleaned_page_docs),
                    "total_classified_chunks": len(chunk_docs),
                    "total_llm_ready_chunks": len(llm_ready_docs),
                    "error_message": None,
                    "processing_finished_at": now,
                    "updated_at": now,
                    "lease_until": None,
                    "timings_ms": dict(timings_ms),
                    "preparation_summary": prep_summary,
                },
                "$unset": {"processing_token": ""},
            },
        )
    except PyMongoError:
        return _mark_failed(
            metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
            "Could not save the extracted PDF data. You can retry processing.", current_page_count,
            timings_ms, processing_started,
        )
    except Exception as exc:
        _logger.exception("Unexpected error during PDF processing: %s", exc)
        return _mark_failed(
            metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
            "PDF processing failed unexpectedly. You can retry processing.", current_page_count,
            timings_ms, processing_started,
        )

    final_metadata = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
    _log_timings(
        final_metadata.get("status", "unknown"),
        current_page_count,
        final_metadata.get("total_chunks", 0),
        final_metadata.get("timings_ms", timings_ms),
    )
    return _summary(final_metadata)


def get_extraction_status(user, tender_id):
    tender = _find_owned_tender(user, tender_id)
    metadata_collection, _, _ = _collections()
    metadata = metadata_collection.find_one({
        "user_id": user["_id"],
        "tender_id": tender["_id"],
        "file_hash": tender.get("sha256"),
    })
    return _summary(metadata)


def list_extraction_summaries(user):
    database = get_database(current_app)
    tender_rows = list(database.tenders.find(
        {"owner_user_id": user["_id"]}, {"_id": 1, "sha256": 1}
    ))
    tender_by_id = {row["_id"]: row for row in tender_rows}
    metadata_collection, _, _ = _collections()
    summaries = {}
    for metadata in metadata_collection.find({
        "user_id": user["_id"],
        "tender_id": {"$in": list(tender_by_id)},
    }):
        tender = tender_by_id.get(metadata["tender_id"])
        if tender and tender.get("sha256") == metadata.get("file_hash"):
            summaries[str(metadata["tender_id"])] = _summary(metadata)
    return summaries


def get_cleaned_pages(user, tender_id):
    """Retrieve cleaned page representations for a tender."""
    tender = _find_owned_tender(user, tender_id)
    _, _, _, cleaned_pages_col, _ = _all_collections()
    return list(cleaned_pages_col.find(
        {"user_id": user["_id"], "tender_id": tender["_id"], "file_hash": tender.get("sha256")}
    ).sort("page_number", ASCENDING))


def get_llm_ready_chunks(user, tender_id):
    """Retrieve curated LLM-ready candidate chunks for a tender."""
    tender = _find_owned_tender(user, tender_id)
    _, _, _, _, llm_ready_col = _all_collections()
    return list(llm_ready_col.find(
        {"user_id": user["_id"], "tender_id": tender["_id"], "file_hash": tender.get("sha256")}
    ).sort("chunk_index", ASCENDING))


def get_chunk_source_text(user, tender_id, chunk_index):
    """Retrieve the original extracted raw page text for a given chunk index."""
    tender = _find_owned_tender(user, tender_id)
    _, pages_col, chunks_col, _, _ = _all_collections()
    chunk = chunks_col.find_one({
        "user_id": user["_id"],
        "tender_id": tender["_id"],
        "file_hash": tender.get("sha256"),
        "chunk_index": chunk_index,
    })
    if not chunk:
        return ""
    page_rows = list(pages_col.find({
        "user_id": user["_id"],
        "tender_id": tender["_id"],
        "file_hash": tender.get("sha256"),
        "page_number": chunk.get("page_number", 1),
    }).sort("part_index", ASCENDING))
    return "\n".join(row.get("text", "") for row in page_rows)
