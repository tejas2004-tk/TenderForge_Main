import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4

import fitz
from flask import current_app
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from database import get_database
from services.tender_service import TenderError, _find_owned_tender, _tender_directory

CHUNK_SIZE = 4000
CHUNK_OVERLAP = 300
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
                _INDEXES_READY = True
    return (
        database.tender_extractions,
        database.tender_extracted_pages,
        database.tender_extracted_chunks,
    )


def split_page_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split a page into overlapping chunks without dropping characters."""
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
            "pdf_text_extraction_ms=%.1f scanned_detection_ms=%.1f chunk_creation_ms=%.1f "
            "mongo_page_writes_ms=%.1f mongo_chunk_writes_ms=%.1f total_ms=%.1f",
            status,
            page_count,
            total_chunks,
            timings_ms["sha256_calculation_ms"],
            timings_ms["pdf_text_extraction_ms"],
            timings_ms["scanned_pdf_detection_ms"],
            timings_ms["chunk_creation_ms"],
            timings_ms["mongodb_page_writes_ms"],
            timings_ms["mongodb_chunk_writes_ms"],
            timings_ms["total_processing_ms"],
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
        set_values.update({"page_count": 0, "extracted_pages": 0, "total_chunks": 0})

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


def _page_text(page):
    return page.get_text("text") or ""


def process_tender(user, tender_id):
    processing_started = time.perf_counter()
    timings_ms = {
        "sha256_calculation_ms": 0.0,
        "pdf_text_extraction_ms": 0.0,
        "scanned_pdf_detection_ms": 0.0,
        "chunk_creation_ms": 0.0,
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
    metadata_collection, pages, chunks = _collections()
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
        # Old versions are cleaned once; a new extraction has no page/chunk rows to delete.
        pages.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})
        chunks.delete_many({"user_id": user_id, "tender_id": tender_object_id, "file_hash": {"$ne": file_hash}})

    current_page_count = 0
    try:
        completed_pages = set()
        next_chunk_index = 0
        if previous_hash == file_hash:
            completed_pages = set(pages.distinct(
                "page_number", {**version_query, "part_index": 0, "complete": True}
            ))
            incomplete_pages = pages.distinct(
                "page_number", {**version_query, "complete": {"$ne": True}}
            )
            if incomplete_pages:
                incomplete_query = {**version_query, "page_number": {"$in": incomplete_pages}}
                pages.delete_many(incomplete_query)
                chunks.delete_many(incomplete_query)
            last_chunk = chunks.find_one(
                version_query, sort=[("chunk_index", DESCENDING)], projection={"chunk_index": 1}
            )
            next_chunk_index = last_chunk["chunk_index"] + 1 if last_chunk else 0

        page_docs, chunk_docs, completed_page_numbers = [], [], []
        batch_bytes = 0

        def flush_batch():
            nonlocal page_docs, chunk_docs, completed_page_numbers, batch_bytes
            if not page_docs and not chunk_docs and not completed_page_numbers:
                return True
            now = datetime.now(timezone.utc)
            # Lease validation and renewal once per batch preserves ownership without page-loop reads.
            lease = metadata_collection.update_one(
                {"user_id": user_id, "tender_id": tender_object_id,
                 "file_hash": file_hash, "processing_token": token},
                {"$set": {"lease_until": now + PROCESSING_LEASE, "updated_at": now}},
            )
            if lease.matched_count != 1:
                return False
            if page_docs:
                write_started = time.perf_counter()
                pages.insert_many(page_docs, ordered=True)
                timings_ms["mongodb_page_writes_ms"] += (time.perf_counter() - write_started) * 1000
            if chunk_docs:
                write_started = time.perf_counter()
                chunks.insert_many(chunk_docs, ordered=True)
                timings_ms["mongodb_chunk_writes_ms"] += (time.perf_counter() - write_started) * 1000
            if completed_page_numbers:
                write_started = time.perf_counter()
                pages.update_many(
                    {"user_id": user_id, "tender_id": tender_object_id, "file_hash": file_hash,
                     "page_number": {"$in": completed_page_numbers}},
                    {"$set": {"complete": True}},
                )
                timings_ms["mongodb_page_writes_ms"] += (time.perf_counter() - write_started) * 1000
            page_docs, chunk_docs, completed_page_numbers = [], [], []
            batch_bytes = 0
            return True

        try:
            document = fitz.open(file_path)
        except Exception:
            return _mark_failed(
                metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                "Could not read this PDF. It may be corrupted or unsupported.", 0,
                timings_ms, processing_started,
            )

        with document:
            if document.needs_pass:
                return _mark_failed(
                    metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                    "This PDF is password-protected and cannot be processed.", 0,
                    timings_ms, processing_started,
                )

            current_page_count = document.page_count
            metadata_collection.update_one(
                {"user_id": user_id, "tender_id": tender_object_id, "processing_token": token},
                {"$set": {"page_count": current_page_count, "updated_at": datetime.now(timezone.utc)}},
            )
            if current_page_count == 0:
                return _mark_failed(
                    metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                    "This PDF does not contain any pages.", 0,
                    timings_ms, processing_started,
                )

            for page_index in range(current_page_count):
                page_number = page_index + 1
                if page_number in completed_pages:
                    continue
                try:
                    page = document.load_page(page_index)
                    stage_started = time.perf_counter()
                    text = _page_text(page)
                    timings_ms["pdf_text_extraction_ms"] += (time.perf_counter() - stage_started) * 1000
                    stage_started = time.perf_counter()
                    page_area = max(page.rect.get_area(), 1)
                    has_page_image = any(
                        fitz.Rect(image_info["bbox"]).get_area() / page_area >= 0.25
                        for image_info in page.get_image_info()
                    )
                    timings_ms["scanned_pdf_detection_ms"] += (time.perf_counter() - stage_started) * 1000
                except Exception:
                    return _mark_failed(
                        metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
                        f"Text extraction failed on page {page_number}. You can retry processing.", current_page_count,
                        timings_ms, processing_started,
                    )

                character_count = len(text.strip())
                part_count = max(1, (len(text) + PAGE_TEXT_PART_SIZE - 1) // PAGE_TEXT_PART_SIZE)
                for part_index, part in enumerate(_iter_page_storage_text(text)):
                    page_docs.append({
                        "user_id": user_id, "tender_id": tender_object_id, "file_hash": file_hash,
                        "page_number": page_number, "part_index": part_index,
                        "part_count": part_count, "text_character_count": character_count,
                        "has_page_image": has_page_image, "text": part, "complete": False,
                    })
                    batch_bytes += len(part.encode("utf-8"))
                    if batch_bytes >= WRITE_BATCH_BYTE_LIMIT:
                        if not flush_batch():
                            latest = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
                            return _summary(latest, in_progress=True)
                chunk_started = time.perf_counter()
                for chunk_text in _iter_page_chunks(text):
                    chunk_docs.append({
                        "user_id": user_id, "tender_id": tender_object_id, "file_hash": file_hash,
                        "chunk_index": next_chunk_index, "page_number": page_number,
                        "original_page_numbers": [page_number], "text": chunk_text,
                    })
                    next_chunk_index += 1
                    batch_bytes += len(chunk_text.encode("utf-8"))
                    if batch_bytes >= WRITE_BATCH_BYTE_LIMIT:
                        timings_ms["chunk_creation_ms"] += (time.perf_counter() - chunk_started) * 1000
                        if not flush_batch():
                            latest = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
                            return _summary(latest, in_progress=True)
                        chunk_started = time.perf_counter()
                timings_ms["chunk_creation_ms"] += (time.perf_counter() - chunk_started) * 1000
                completed_page_numbers.append(page_number)
                if (len(completed_page_numbers) >= WRITE_BATCH_PAGE_COUNT
                        or batch_bytes >= WRITE_BATCH_BYTE_LIMIT):
                    if not flush_batch():
                        latest = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
                        return _summary(latest, in_progress=True)

            if not flush_batch():
                latest = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
                return _summary(latest, in_progress=True)

        extracted_pages, total_chunks = _counts(user_id, tender_object_id, file_hash, pages, chunks)
        page_stats = pages.find(
            {**version_query, "part_index": 0, "complete": True},
            {"text_character_count": 1, "has_page_image": 1},
        )
        scanned_pages = 0
        total_text_characters = 0
        for item in page_stats:
            scanned_pages += item.get("text_character_count", 0) < LOW_TEXT_CHARACTER_LIMIT and item.get("has_page_image", False)
            total_text_characters += item.get("text_character_count", 0)
        needs_ocr = current_page_count > 0 and scanned_pages > current_page_count / 2
        now = datetime.now(timezone.utc)
        if needs_ocr:
            status = "needs_ocr"
            error_message = "This PDF appears to be scanned and cannot currently be processed without OCR."
        elif total_text_characters == 0:
            status = "failed"
            error_message = "This PDF contains no extractable text. It may be blank or scanned in an unsupported format."
        else:
            status = "completed"
            error_message = None
        timings_ms["total_processing_ms"] = (time.perf_counter() - processing_started) * 1000
        metadata_collection.update_one(
            {"user_id": user_id, "tender_id": tender_object_id, "file_hash": file_hash,
             "processing_token": token},
            {"$set": {
                "status": status, "page_count": current_page_count,
                "extracted_pages": extracted_pages, "total_chunks": total_chunks,
                "error_message": error_message, "processing_finished_at": now,
                "updated_at": now, "lease_until": None, "timings_ms": dict(timings_ms),
            }, "$unset": {"processing_token": ""}},
        )
    except PyMongoError:
        return _mark_failed(
            metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
            "Could not save the extracted PDF data. You can retry processing.", current_page_count,
            timings_ms, processing_started,
        )
    except Exception:
        return _mark_failed(
            metadata_collection, pages, chunks, user_id, tender_object_id, file_hash, token,
            "PDF processing failed unexpectedly. You can retry processing.", current_page_count,
            timings_ms, processing_started,
        )

    final_metadata = metadata_collection.find_one({"user_id": user_id, "tender_id": tender_object_id})
    _log_timings(final_metadata.get("status", "unknown"), current_page_count,
                 final_metadata.get("total_chunks", 0), final_metadata.get("timings_ms", timings_ms))
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
