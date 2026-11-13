import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from bson import ObjectId
from flask import current_app
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
from werkzeug.utils import secure_filename

from database import get_database

PDF_HEADER = b"%PDF-"
PDF_EOF = b"%%EOF"
_STORAGE_NAME = re.compile(r"^[0-9a-f]{32}\.pdf$")
_INDEX_LOCK = Lock()
_INDEXES_READY = False


class TenderError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _collection():
    global _INDEXES_READY
    tenders = get_database(current_app).tenders
    if not _INDEXES_READY:
        with _INDEX_LOCK:
            if not _INDEXES_READY:
                tenders.create_index(
                    [("owner_user_id", ASCENDING), ("sha256", ASCENDING)],
                    unique=True,
                    name="unique_tender_per_owner_and_content",
                )
                tenders.create_index(
                    [("owner_user_id", ASCENDING), ("uploaded_at", DESCENDING)],
                    name="tenders_by_owner_and_date",
                )
                _INDEXES_READY = True
    return tenders


def _owner_id(user):
    return user["_id"]


def _public_tender(tender):
    return {
        "id": str(tender["_id"]),
        "filename": tender["filename"],
        "size": tender["size"],
        "uploaded_at": tender["uploaded_at"].isoformat(),
        "status": tender.get("status", "Uploaded"),
    }


def _tender_directory():
    return Path(current_app.root_path) / "uploads" / "tenders"


def _validate_pdf(file, max_size_mb):
    if file is None or not file.filename:
        raise TenderError("Choose a PDF file to upload.")
    if Path(file.filename).suffix.lower() != ".pdf":
        raise TenderError("Only PDF files are accepted.")

    size_limit = max_size_mb * 1024 * 1024
    if size_limit < 1:
        raise TenderError("The tender upload size limit is not configured correctly.", 503)
    content = file.stream.read(size_limit + 1)
    if not content:
        raise TenderError("The selected PDF is empty.")
    if len(content) > size_limit:
        raise TenderError(f"The PDF exceeds the {max_size_mb} MB upload limit.", 413)
    if not content.startswith(PDF_HEADER) or PDF_EOF not in content[-2048:]:
        raise TenderError("The selected file is not a valid PDF.")
    return content


def upload_tender(user, file, max_size_mb):
    content = _validate_pdf(file, max_size_mb)
    safe_filename = secure_filename(file.filename)
    if not safe_filename:
        safe_filename = "tender.pdf"

    content_hash = hashlib.sha256(content).hexdigest()
    tenders = _collection()
    existing = tenders.find_one({"owner_user_id": _owner_id(user), "sha256": content_hash})
    if existing:
        return _public_tender(existing), True

    directory = _tender_directory()
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid4().hex}.pdf"
    file_path = directory / storage_name
    file_path.write_bytes(content)

    tender = {
        "owner_user_id": _owner_id(user),
        "filename": safe_filename,
        "storage_name": storage_name,
        "size": len(content),
        "sha256": content_hash,
        "uploaded_at": datetime.now(timezone.utc),
        "status": "Uploaded",
    }
    try:
        result = tenders.insert_one(tender)
    except PyMongoError:
        file_path.unlink(missing_ok=True)
        raise

    tender["_id"] = result.inserted_id
    return _public_tender(tender), False


def list_tenders(user):
    tenders = _collection().find({"owner_user_id": _owner_id(user)}).sort("uploaded_at", DESCENDING)
    return [_public_tender(tender) for tender in tenders]


def _find_owned_tender(user, tender_id):
    if not ObjectId.is_valid(tender_id):
        raise TenderError("Tender not found.", 404)
    tender = _collection().find_one({"_id": ObjectId(tender_id), "owner_user_id": _owner_id(user)})
    if not tender:
        raise TenderError("Tender not found.", 404)
    storage_name = tender.get("storage_name", "")
    if not _STORAGE_NAME.fullmatch(storage_name):
        raise TenderError("Tender file could not be found.", 404)
    return tender


def download_tender(user, tender_id):
    tender = _find_owned_tender(user, tender_id)
    directory = _tender_directory()
    if not (directory / tender["storage_name"]).is_file():
        raise TenderError("Tender file could not be found.", 404)
    return {**tender, "directory": str(directory)}


def delete_tender(user, tender_id):
    tenders = _collection()
    tender = _find_owned_tender(user, tender_id)
    file_path = _tender_directory() / tender["storage_name"]
    original_content = file_path.read_bytes() if file_path.is_file() else None
    if original_content is not None:
        file_path.unlink()

    try:
        result = tenders.delete_one({"_id": tender["_id"], "owner_user_id": _owner_id(user)})
    except PyMongoError:
        if original_content is not None:
            file_path.write_bytes(original_content)
        raise
    if result.deleted_count != 1:
        if original_content is not None:
            file_path.write_bytes(original_content)
        raise TenderError("Tender not found.", 404)
