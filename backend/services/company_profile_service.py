import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from bson import ObjectId
from flask import current_app
from pymongo import ASCENDING
from pymongo.errors import PyMongoError
from werkzeug.utils import secure_filename

from database import get_database

PROFILE_FIELDS = {
    "legal_name": 200,
    "trade_name": 200,
    "website": 300,
    "description": 5000,
    "registration_type": 120,
    "registration_number": 120,
    "incorporation_date": 40,
    "contact_name": 160,
    "contact_role": 120,
    "contact_email": 254,
    "contact_phone": 60,
    "alternate_phone": 60,
    "address": 500,
    "city": 120,
    "state": 120,
    "postal_code": 30,
    "country": 120,
    "gst_number": 40,
    "pan_number": 20,
    "turnover_current_year": 20,
    "turnover_current_amount": 40,
    "turnover_previous_year": 20,
    "turnover_previous_amount": 40,
    "years_experience": 20,
    "sectors": 3000,
    "employee_count": 20,
    "services": 3000,
    "previous_projects": 5000,
    "certifications": 3000,
}
ALLOWED_DOCUMENT_TYPES = {"pdf", "doc", "docx", "xls", "xlsx", "jpg", "jpeg", "png"}
MAX_DOCUMENT_SIZE = 10 * 1024 * 1024
MAX_DOCUMENTS_PER_UPLOAD = 5
_INDEX_READY = False


class ProfileError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _profile_collection():
    global _INDEX_READY
    profiles = get_database(current_app).company_profiles
    if not _INDEX_READY:
        profiles.create_index([("owner_user_id", ASCENDING)], unique=True, name="unique_profile_owner")
        _INDEX_READY = True
    return profiles


def _owner_id(user):
    return user["_id"]


def _empty_profile(user):
    fields = {field: "" for field in PROFILE_FIELDS}
    fields["legal_name"] = user.get("company_name", "")
    return {
        **fields,
        "supporting_documents": [],
    }


def _public_document(document):
    return {
        "id": document["id"],
        "name": document["name"],
        "size": document["size"],
        "uploaded_at": document["uploaded_at"].isoformat(),
    }


def _public_profile(profile, user):
    if not profile:
        return _empty_profile(user)
    output = {field: profile.get(field, "") for field in PROFILE_FIELDS}
    output["supporting_documents"] = [
        _public_document(document) for document in profile.get("supporting_documents", [])
    ]
    return output


def get_company_profile(user):
    profile = _profile_collection().find_one({"owner_user_id": _owner_id(user)})
    return _public_profile(profile, user)


def save_company_profile(user, data):
    if not isinstance(data, dict):
        raise ProfileError("Send the company profile as JSON.")

    updates = {}
    for field, max_length in PROFILE_FIELDS.items():
        value = data.get(field, "")
        if not isinstance(value, str):
            raise ProfileError(f"{field.replace('_', ' ').capitalize()} must be text.")
        value = value.strip()
        if len(value) > max_length:
            raise ProfileError(f"{field.replace('_', ' ').capitalize()} is too long.")
        updates[field] = value

    now = datetime.now(timezone.utc)
    profiles = _profile_collection()
    profiles.update_one(
        {"owner_user_id": _owner_id(user)},
        {
            "$set": {**updates, "updated_at": now},
            "$setOnInsert": {
                "owner_user_id": _owner_id(user),
                "created_at": now,
                "supporting_documents": [],
            },
        },
        upsert=True,
    )
    return get_company_profile(user)


def _user_upload_directory(user):
    directory = Path(current_app.root_path) / "uploads" / "company_profiles" / str(_owner_id(user))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _content_matches_type(extension, content):
    """Check file signatures so a renamed executable cannot pass the allowlist."""
    if extension == "pdf":
        return content.startswith(b"%PDF-")
    if extension == "png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {"jpg", "jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension in {"doc", "xls"}:
        # Legacy Word and Excel documents use the OLE compound-file signature.
        return content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if extension in {"docx", "xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = set(archive.namelist())
            expected_member = "word/document.xml" if extension == "docx" else "xl/workbook.xml"
            return "[Content_Types].xml" in members and expected_member in members
        except (OSError, zipfile.BadZipFile):
            return False
    return False


def save_supporting_documents(user, uploads):
    uploads = [upload for upload in uploads if upload and upload.filename]
    if not uploads:
        raise ProfileError("Choose at least one supporting document.")
    if len(uploads) > MAX_DOCUMENTS_PER_UPLOAD:
        raise ProfileError("Upload no more than five supporting documents at once.")

    prepared = []
    directory = _user_upload_directory(user)
    try:
        for upload in uploads:
            original_name = secure_filename(upload.filename)
            extension = Path(original_name).suffix.lower().lstrip(".")
            if not original_name or extension not in ALLOWED_DOCUMENT_TYPES:
                raise ProfileError("Use PDF, Word, Excel, JPG, or PNG supporting documents.")

            content = upload.stream.read(MAX_DOCUMENT_SIZE + 1)
            if not content:
                raise ProfileError(f"{original_name} is empty.")
            if len(content) > MAX_DOCUMENT_SIZE:
                raise ProfileError(f"Each supporting document must be 10 MB or smaller.")
            if not _content_matches_type(extension, content):
                raise ProfileError(f"{original_name} does not match its file extension.")

            storage_name = f"{uuid4().hex}.{extension}"
            path = directory / storage_name
            path.write_bytes(content)
            prepared.append({
                "id": uuid4().hex,
                "name": original_name,
                "storage_name": storage_name,
                "size": len(content),
                "uploaded_at": datetime.now(timezone.utc),
                "path": path,
            })

        stored_documents = [{key: value for key, value in document.items() if key != "path"} for document in prepared]
        now = datetime.now(timezone.utc)
        _profile_collection().update_one(
            {"owner_user_id": _owner_id(user)},
            {
                "$push": {"supporting_documents": {"$each": stored_documents}},
                "$set": {"updated_at": now},
                "$setOnInsert": {"owner_user_id": _owner_id(user), "created_at": now},
            },
            upsert=True,
        )
        return [_public_document(document) for document in stored_documents]
    except (ProfileError, OSError, PyMongoError):
        for document in prepared:
            document["path"].unlink(missing_ok=True)
        raise


def find_supporting_document(user, document_id):
    profile = _profile_collection().find_one({"owner_user_id": _owner_id(user)})
    if profile:
        for document in profile.get("supporting_documents", []):
            if document.get("id") == document_id:
                return {
                    **document,
                    "directory": str(_user_upload_directory(user)),
                }
    raise ProfileError("Supporting document not found.", 404)


def delete_supporting_document(user, document_id):
    document = find_supporting_document(user, document_id)
    _profile_collection().update_one(
        {"owner_user_id": _owner_id(user)},
        {
            "$pull": {"supporting_documents": {"id": document_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    path = Path(document["directory"]) / document["storage_name"]
    path.unlink(missing_ok=True)
