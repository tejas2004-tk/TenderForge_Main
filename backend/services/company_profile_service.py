import io
import hashlib
import re
import json
import unicodedata
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
from services.tender_service import TenderError, _find_owned_tender

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
    "company_type": 120,
    "year_of_establishment": 10,
    "office_address": 500,
    "company_email": 254,
    "company_phone": 60,
    "cin_llpin": 40,
    "udyam_msme": 40,
    "other_registration_numbers": 3000,
    "annual_turnover": 40,
    "net_worth": 40,
    "financial_year": 20,
    "audited_financial_information": 3000,
    "core_business_activities": 3000,
    "product_categories": 3000,
    "industries_sectors_served": 3000,
}
PAST_PROJECT_FIELDS = {
    "project_name": 200,
    "client_department": 200,
    "work_description": 5000,
    "contract_value": 40,
    "completion_date": 40,
    "supporting_document_id": 100,
}
CERTIFICATION_FIELDS = {
    "certification_name": 200,
    "issuing_authority": 200,
    "certificate_number": 120,
    "issue_date": 40,
    "expiry_date": 40,
    "supporting_document_id": 100,
}
MAX_PROFILE_ENTRIES = 100
_RESPONSE_INDEX_READY = False
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
        "past_projects": [],
        "certification_entries": [],
    }


def _public_document(document):
    return {
        "id": document["id"],
        "name": document["name"],
        "size": document["size"],
        "uploaded_at": document["uploaded_at"].isoformat(),
    }

def _public_entries(entries, fields):
    if not isinstance(entries, list):
        return []
    output = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        output.append({
            field: entry.get(field, "") if isinstance(entry.get(field, ""), str) else ""
            for field in fields
        })
    return output

def _validate_entries(data, field_name, fields):
    entries = data.get(field_name)
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise ProfileError(f"{field_name.replace('_', ' ').capitalize()} must be a list.")
    if len(entries) > MAX_PROFILE_ENTRIES:
        raise ProfileError(f"Keep no more than {MAX_PROFILE_ENTRIES} entries.")

    validated = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProfileError("Each reusable profile entry must be an object.")
        clean = {}
        for field, max_length in fields.items():
            value = entry.get(field, "")
            if not isinstance(value, str):
                raise ProfileError(f"{field.replace('_', ' ').capitalize()} must be text.")
            value = value.strip()
            if len(value) > max_length:
                raise ProfileError(f"{field.replace('_', ' ').capitalize()} is too long.")
            clean[field] = value
        validated.append(clean)
    return validated

def _public_profile(profile, user):
    if not profile:
        return _empty_profile(user)
    output = {field: profile.get(field, "") for field in PROFILE_FIELDS}
    output["supporting_documents"] = [
        _public_document(document) for document in profile.get("supporting_documents", [])
    ]
    output["past_projects"] = _public_entries(profile.get("past_projects"), PAST_PROJECT_FIELDS)
    output["certification_entries"] = _public_entries(profile.get("certification_entries"), CERTIFICATION_FIELDS)
    return output


def get_company_profile(user):
    profile = _profile_collection().find_one({"owner_user_id": _owner_id(user)})
    return _public_profile(profile, user)


def save_company_profile(user, data):
    if not isinstance(data, dict):
        raise ProfileError("Send the company profile as JSON.")

    updates = {}
    for field, max_length in PROFILE_FIELDS.items():
        if field not in data:
            continue
        value = data.get(field, "")
        if not isinstance(value, str):
            raise ProfileError(f"{field.replace('_', ' ').capitalize()} must be text.")
        value = value.strip()
        if len(value) > max_length:
            raise ProfileError(f"{field.replace('_', ' ').capitalize()} is too long.")
        updates[field] = value

    past_projects = _validate_entries(data, "past_projects", PAST_PROJECT_FIELDS)
    if past_projects is not None:
        updates["past_projects"] = past_projects
    certifications = _validate_entries(data, "certification_entries", CERTIFICATION_FIELDS)
    if certifications is not None:
        updates["certification_entries"] = certifications

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


def _tender_response_collection():
    global _RESPONSE_INDEX_READY
    responses = get_database(current_app).tender_company_responses
    if not _RESPONSE_INDEX_READY:
        responses.create_index(
            [("owner_user_id", ASCENDING), ("tender_id", ASCENDING), ("requirement_id", ASCENDING)],
            unique=True,
            name="unique_tender_requirement_response",
        )
        responses.create_index(
            [("owner_user_id", ASCENDING), ("tender_id", ASCENDING)],
            name="tender_responses_by_owner",
        )
        _RESPONSE_INDEX_READY = True
    return responses


def _public_response_document(document):
    return {
        "id": document["id"],
        "name": document["name"],
        "size": document["size"],
        "uploaded_at": document["uploaded_at"].isoformat(),
    }


def _response_timestamp(row):
    value = row.get("updated_at")
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _load_actionable_tender_data(user, tender_id):
    from services.tender_analysis_service import get_analysis

    tender = _find_owned_tender(user, tender_id)
    profile = get_company_profile(user)
    analysis = get_analysis(user, tender_id)
    matches = match_tender_requirements(profile, analysis)
    actionable, excluded = classify_and_group_tender_requirements(matches)
    return tender, analysis, actionable, excluded


def get_tender_responses(user, tender_id):
    tender, analysis, actionable, excluded = _load_actionable_tender_data(user, tender_id)
    collection = _tender_response_collection()
    saved_rows = list(collection.find({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
    }))
    saved_by_requirement = {row["requirement_id"]: row for row in saved_rows}

    cards = []
    completed = 0
    for requirement in actionable:
        requirement_id = requirement["requirement_id"]
        legacy_ids = requirement.get("original_requirement_ids", [])
        saved = saved_by_requirement.get(requirement_id)
        legacy_rows = [saved_by_requirement[item_id] for item_id in legacy_ids if item_id in saved_by_requirement]
        if saved is None:
            saved = max(legacy_rows, key=_response_timestamp) if legacy_rows else None
        documents = []
        if saved:
            for legacy_id in legacy_ids:
                legacy = saved_by_requirement.get(legacy_id)
                if legacy:
                    for document in legacy.get("documents", []):
                        if document not in documents:
                            documents.append(document)
            for document in saved.get("documents", []):
                if document not in documents:
                    documents.append(document)
        response_data = saved.get("response_data", {}) if saved else {}
        preserved_responses = [
            {"requirement_id": row["requirement_id"], "response_data": row.get("response_data", {})}
            for row in legacy_rows
            if row.get("requirement_id") != (saved or {}).get("requirement_id") and row.get("response_data")
        ]
        original_requirements = requirement.get("original_requirements", [])
        if requirement["match_status"] == "satisfied":
            status = "completed"
        elif requirement["match_status"] == "review_required":
            status = "review_required"
        elif saved_by_requirement.get(requirement_id):
            complete = saved.get("status") == "completed" and _response_is_complete(
                requirement["match_status"], requirement.get("suggested_input_type"), response_data, documents,
            ) and not requirement.get("completion_requires_profile_match") and _group_documents_complete(original_requirements, documents)
            status = "completed" if complete else "missing"
        elif original_requirements:
            incomplete_originals = [
                original for original in original_requirements
                if original.get("match_status") != "satisfied" and not (
                    not original.get("completion_requires_profile_match") and
                    saved_by_requirement.get(original.get("requirement_id"), {}).get("status") == "completed"
                )
            ]
            status = "completed" if not incomplete_originals and _group_documents_complete(original_requirements, documents) else "missing"
        else:
            status = "completed" if saved and saved.get("status") == "completed" else "missing"
        if status == "completed":
            completed += 1
        cards.append({
            **requirement,
            "current_status": status,
            "response_data": response_data,
            "preserved_responses": preserved_responses,
            "documents": [_public_response_document(document) for document in documents],
            "updated_at": saved.get("updated_at").isoformat() if saved and saved.get("updated_at") else None,
        })

    return {
        "tender": {
            "id": str(tender["_id"]),
            "filename": tender["filename"],
        },
        "analysis_status": analysis.get("status", "not_started"),
        "cards": cards,
        "progress": {
            "total": len(cards),
            "completed": completed,
            "incomplete": len(cards) - completed,
        },
        "excluded_summary": excluded,
    }


def _response_is_complete(match_status, suggested_input_type, response_data, documents):
    if match_status == "document_needed":
        return len(documents) > 0
    if match_status == "review_required":
        return False
    if suggested_input_type in {"number", "text", "selection"}:
        value = response_data.get("value")
        return isinstance(value, str) and bool(value.strip()) or isinstance(value, (int, float)) and not isinstance(value, bool)
    if suggested_input_type in {"confirmation", "yes_no"}:
        return response_data.get("confirmed") is True or response_data.get("confirmed") is False
    return bool(response_data.get("value")) or response_data.get("confirmed") is True


def _group_documents_complete(requirements, documents):
    document_requirements = [item for item in requirements if item.get("match_status") == "document_needed"]
    if not document_requirements:
        return True
    document_profile = {"supporting_documents": documents}
    for requirement in document_requirements:
        text = _classification_text(requirement)
        if _match_phrase(text, "pan") and _match_phrase(text, "gst"):
            terms = [["pan"], ["gst"]]
        elif _match_phrase(text, "pan"):
            terms = [["pan"]]
        elif _match_phrase(text, "gst") or _match_phrase(text, "gstin"):
            terms = [["gst"]]
        elif re.search(r"\b(?:audited|financial statement|turnover statement)\b", text, re.I):
            terms = [["audited financial", "audited statement", "financial statement", "turnover statement", "audit report"]]
        elif re.search(r"\b(?:incorporation|registration)\b", text, re.I):
            terms = [["incorporation", "registration"]]
        else:
            terms = [[]]
        if any(aliases and not _document_available(document_profile, aliases) for aliases in terms):
            return False
        if not documents:
            return False
    return True


def save_tender_response(user, tender_id, data):
    if not isinstance(data, dict):
        raise TenderError("Send the tender response as JSON.")
    requirement_id = data.get("requirement_id")
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise TenderError("A requirement identifier is required.")
    requirement_id = requirement_id.strip()

    tender, _, actionable, _ = _load_actionable_tender_data(user, tender_id)
    match = next((item for item in actionable if item["requirement_id"] == requirement_id), None)
    if not match:
        raise TenderError("This requirement is not actionable for the selected tender.", 404)

    response_data = data.get("response_data")
    if not isinstance(response_data, dict):
        raise TenderError("Send response_data as an object.")
    clean_response = {}
    if "value" in response_data:
        value = response_data["value"]
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise TenderError("The response value must be text or a number.")
        value = str(value).strip()
        if len(value) > 5000:
            raise TenderError("The response is too long.")
        clean_response["value"] = value
    if "confirmed" in response_data:
        confirmed = response_data["confirmed"]
        if not isinstance(confirmed, bool):
            raise TenderError("Confirmation must be yes or no.")
        clean_response["confirmed"] = confirmed

    collection = _tender_response_collection()
    existing = collection.find_one({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
        "requirement_id": requirement_id,
    })
    documents = existing.get("documents", []) if existing else []
    complete = _response_is_complete(
        match["match_status"],
        match.get("suggested_input_type"),
        clean_response,
        documents,
    )
    if match.get("completion_requires_profile_match"):
        complete = False
    now = datetime.now(timezone.utc)
    collection.update_one(
        {
            "owner_user_id": _owner_id(user),
            "tender_id": tender["_id"],
            "requirement_id": requirement_id,
        },
        {
            "$set": {
                "response_data": clean_response,
                "status": "completed" if complete else "missing",
                "match_status": match["match_status"],
                "updated_at": now,
            },
            "$setOnInsert": {
                "owner_user_id": _owner_id(user),
                "tender_id": tender["_id"],
                "requirement_id": requirement_id,
                "documents": [],
                "created_at": now,
            },
        },
        upsert=True,
    )
    return get_tender_responses(user, tender_id)


def delete_tender_response(user, tender_id, requirement_id):
    tender = _find_owned_tender(user, tender_id)
    collection = _tender_response_collection()
    saved = collection.find_one({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
        "requirement_id": requirement_id,
    })
    if saved:
        directory = _tender_response_directory(user, tender["_id"], requirement_id)
        for document in saved.get("documents", []):
            path = directory / document["storage_name"]
            path.unlink(missing_ok=True)
    collection.delete_many({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
        "requirement_id": requirement_id,
    })
    return get_tender_responses(user, tender_id)


def _tender_response_directory(user, tender_id, requirement_id):
    safe_requirement = hashlib.sha256(str(requirement_id).encode("utf-8")).hexdigest()
    directory = (
        Path(current_app.root_path) / "uploads" / "tender_responses"
        / str(_owner_id(user)) / str(tender_id) / safe_requirement
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_tender_response_documents(user, tender_id, requirement_id, uploads):
    uploads = [upload for upload in uploads if upload and upload.filename]
    if not uploads:
        raise ProfileError("Choose at least one supporting document.")
    if len(uploads) > MAX_DOCUMENTS_PER_UPLOAD:
        raise ProfileError("Upload no more than five supporting documents at once.")

    tender, _, actionable, _ = _load_actionable_tender_data(user, tender_id)
    if not any(item["requirement_id"] == requirement_id for item in actionable):
        raise ProfileError("This requirement is not actionable for the selected tender.", 404)

    collection = _tender_response_collection()
    saved = collection.find_one({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
        "requirement_id": requirement_id,
    })
    if saved and len(saved.get("documents", [])) + len(uploads) > MAX_DOCUMENTS_PER_UPLOAD:
        raise ProfileError("Keep no more than five tender-response documents per requirement.")

    directory = _tender_response_directory(user, tender["_id"], requirement_id)
    prepared = []
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
                raise ProfileError("Each supporting document must be 10 MB or smaller.")
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

        stored_documents = [
            {key: value for key, value in document.items() if key != "path"}
            for document in prepared
        ]
        now = datetime.now(timezone.utc)
        match_status = next(
            item["match_status"] for item in actionable
            if item["requirement_id"] == requirement_id
        )
        collection.update_one(
            {
                "owner_user_id": _owner_id(user),
                "tender_id": tender["_id"],
                "requirement_id": requirement_id,
            },
            {
                "$push": {"documents": {"$each": stored_documents}},
                "$set": {
                    "match_status": match_status,
                    "status": "completed" if match_status in {"document_needed", "missing_information"} else "missing",
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "owner_user_id": _owner_id(user),
                    "tender_id": tender["_id"],
                    "requirement_id": requirement_id,
                    "response_data": {},
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return get_tender_responses(user, tender_id)
    except (ProfileError, OSError, PyMongoError):
        for document in prepared:
            document["path"].unlink(missing_ok=True)
        raise


def find_tender_response_document(user, tender_id, requirement_id, document_id):
    tender = _find_owned_tender(user, tender_id)
    saved = _tender_response_collection().find_one({
        "owner_user_id": _owner_id(user),
        "tender_id": tender["_id"],
        "requirement_id": requirement_id,
    })
    if saved:
        for document in saved.get("documents", []):
            if document.get("id") == document_id:
                return {
                    **document,
                    "directory": str(_tender_response_directory(
                        user, tender["_id"], requirement_id,
                    )),
                }
    raise ProfileError("Tender response document not found.", 404)


def delete_tender_response_document(user, tender_id, requirement_id, document_id):
    document = find_tender_response_document(user, tender_id, requirement_id, document_id)
    tender = _find_owned_tender(user, tender_id)
    collection = _tender_response_collection()
    collection.update_one(
        {
            "owner_user_id": _owner_id(user),
            "tender_id": tender["_id"],
            "requirement_id": requirement_id,
        },
        {
            "$pull": {"documents": {"id": document_id}},
            "$set": {"status": "missing", "updated_at": datetime.now(timezone.utc)},
        },
    )
    path = Path(document["directory"]) / document["storage_name"]
    path.unlink(missing_ok=True)
    return get_tender_responses(user, tender_id)


def _classification_text(requirement):
    return " ".join(str(requirement.get(field, "") or "") for field in (
        "requirement_title", "description", "requirement_value",
        "applicability_condition", "evidence_text",
    ))


def classify_and_group_tender_requirements(matches):
    technical_cues = (
        "print speed", "resolution", "dpi", "usb", "ethernet", "duplex",
        "paper capacity", "operating system", "os compatibility", "hardware",
        "software", "processor", "memory", "ram", "screen", "display",
        "printer model", "equipment model", "technical specification",
        "warranty period", "delivery period",
    )
    informational_cues = (
        "submission portal", "bid submission", "submission procedure",
        "submission deadline", "bid opening", "envelope", "cover",
        "general instruction", "tender process", "process instruction",
        "bidder shall visit", "download tender", "portal",
    )
    company_cues = (
        "pan", "gst", "gstin", "udyam", "msme", "company", "firm",
        "registration", "incorporation", "cin", "llpin", "turnover",
        "net worth", "audited", "financial", "solvency", "experience",
        "work order", "completion certificate", "project", "past performance",
        "iso", "certificat", "license", "licence", "authorization",
        "debar", "blacklist", "undertaking", "declaration", "signatory",
        "commitment", "warranty", "performance security", "delivery",
    )

    eligible = []
    technical_count = 0
    informational_count = 0
    uncertain_count = 0

    for requirement in matches:
        text = _classification_text(requirement)
        normalized = _match_normalize(text)
        category = str(requirement.get("category", "") or "")
        status = str(requirement.get("requirement_status", "") or "")
        match_status = requirement.get("match_status")

        if re.search(r"\b(?:submission instructions?|submission deadlines?|procedural requirements?)\b", category, re.I):
            informational_count += 1
            continue

        # Submission logistics describe how and when to bid, not company profile data.
        if re.search(r"\b(two|single|one|separate)[-\s]*(?:envelope|cover)\b|\b(?:envelope|cover) instructions?\b|\bsubmission (?:portal|procedure|process|deadline|instructions?)\b|\bbid (?:submission|opening|opening date|deadline)\b|\bportal\b", text, re.I):
            informational_count += 1
            continue

        if match_status == "not_applicable":
            continue

        if category == "Tender Overview":
            informational_count += 1
            continue

        has_technical = any(_match_phrase(text, cue) for cue in technical_cues)
        has_company = any(_match_phrase(text, cue) for cue in company_cues)
        has_informational = any(_match_phrase(text, cue) for cue in informational_cues)

        if category in {"Technical Requirements", "Contractual Terms", "Procurement Policies"}:
            if has_informational and not has_company:
                informational_count += 1
                continue
        if status in {"informational", "optional"} or has_informational and not has_company:
            informational_count += 1
            continue

        commitment_action = bool(re.search(
            r"\b(?:bidder|tenderer|company|supplier)\s+(?:shall|must|will|is required to)\s+(?:provide|submit|upload|declare|undertake|confirm|accept|ensure|deliver|supply|install|maintain|warrant|furnish|post)\b|\b(?:signed declaration|undertaking|performance security|warranty commitment|delivery commitment|supply and installation commitment)\b",
            text, re.I,
        ))
        if category == "Contractual Terms" and not commitment_action:
            informational_count += 1
            continue
        if category in {"Technical Requirements", "Product and Equipment Specifications"}:
            technical_count += 1
            continue

        if not has_company and not commitment_action:
            if match_status == "review_required":
                uncertain_count += 1
            continue

        if match_status == "satisfied":
            eligible.append(requirement)
        elif category in {"Eligibility Requirements", "Required Documents and Certificates",
                        "Financial Requirements", "Experience Requirements",
                        "EMD and Tender Fees", "Contractual Terms",
                        "Procurement Policies", "Other Mandatory Bidder Conditions"}:
            eligible.append(requirement)
        elif match_status in {"missing_information", "document_needed"}:
            eligible.append(requirement)
        elif match_status == "review_required" and has_company:
            eligible.append(requirement)
        else:
            uncertain_count += 1

    grouped = []
    grouped_by_key = {}
    for requirement in eligible:
        key = _presentation_group_key(requirement)
        financial_details = _financial_group_details(requirement)
        group = next((candidate for candidate in grouped
                      if financial_details and _financial_groups_compatible(candidate["original_requirements"], requirement)), None)
        if group is None and not financial_details:
            group = grouped_by_key.get(key)
        if group is None:
            group = {
                **requirement,
                "tender_category": _user_facing_category(requirement),
                "original_requirements": [],
                "original_requirement_ids": [],
            }
            grouped.append(group)
            if not financial_details:
                grouped_by_key[key] = group
        original = {
            "requirement_id": requirement.get("requirement_id"),
            "category": requirement.get("category", ""),
            "requirement_title": requirement.get("requirement_title", ""),
            "description": requirement.get("description", ""),
            "requirement_value": requirement.get("requirement_value", ""),
            "mandatory": requirement.get("mandatory", False),
            "requirement_status": requirement.get("requirement_status", ""),
            "applicability_condition": requirement.get("applicability_condition", ""),
            "match_status": requirement.get("match_status", ""),
            "reason": requirement.get("reason", ""),
            "matching_profile_fields": requirement.get("matching_profile_fields", []),
            "editable_profile_fields": requirement.get("editable_profile_fields", []),
            "supporting_document_needed": requirement.get("supporting_document_needed", False),
            "completion_requires_profile_match": requirement.get("completion_requires_profile_match", False),
            "confidence": requirement.get("confidence"),
            "validation_status": requirement.get("validation_status", ""),
            "validation_reasons": requirement.get("validation_reasons", []),
            "evidence_warning": requirement.get("evidence_warning"),
            "review_action": requirement.get("review_action"),
            "review_context": requirement.get("review_context"),
            "experience_assessment": requirement.get("experience_assessment"),
            "financial_assessment": requirement.get("financial_assessment"),
            "source_page": requirement.get("source_page"),
            "evidence_text": requirement.get("evidence_text", ""),
            "source_evidence": requirement.get("source_evidence", []),
        }
        group["original_requirements"].append(original)
        if requirement.get("requirement_id"):
            group["original_requirement_ids"].append(requirement["requirement_id"])

        known_sources = group.get("source_evidence", [])
        for source in requirement.get("source_evidence", []):
            if source not in known_sources:
                known_sources.append(source)
        group["source_evidence"] = known_sources
        group["match_status"] = max(
            (item.get("match_status", "missing_information") for item in group["original_requirements"]),
            key=lambda value: {"review_required": 4, "missing_information": 3, "document_needed": 2, "satisfied": 1}.get(value, 0),
        )
        group["reason"] = next((item.get("reason") for item in group["original_requirements"]
                                 if item.get("match_status") == group["match_status"] and item.get("reason")),
                                group.get("reason", ""))
        group["supporting_document_needed"] = any(
            candidate.get("requirement_id") == item.get("requirement_id") and candidate.get("supporting_document_needed")
            for item in group["original_requirements"] for candidate in eligible
        )
        group["additional_user_input_needed"] = group["match_status"] in {"missing_information", "document_needed", "review_required"}
        group["completion_requires_profile_match"] = any(
            item.get("completion_requires_profile_match") for item in group["original_requirements"]
        )
        if group["supporting_document_needed"]:
            group["suggested_input_type"] = "document"
        if "documents_pan_gst" in key:
            group["requirement_title"] = "PAN and GST registration"
        elif "documents_registration_proof" in key:
            group["requirement_title"] = "Incorporation / registration proof"
        if financial_details:
            group["requirement_title"] = "Financial qualification"
            group["financial_subconditions"] = _financial_subconditions(group["original_requirements"])
        group["matching_profile_fields"] = sorted({
            field for item in group["original_requirements"]
            for candidate in eligible if candidate.get("requirement_id") == item.get("requirement_id")
            for field in candidate.get("matching_profile_fields", [])
        })
        group["editable_profile_fields"] = sorted({
            field for item in group["original_requirements"]
            for field in item.get("editable_profile_fields", [])
        })
        warnings = []
        for item in group["original_requirements"]:
            warning = item.get("evidence_warning")
            if warning and warning not in warnings:
                warnings.append(warning)
        group["evidence_warnings"] = warnings
        assessments = [item.get("experience_assessment") for item in group["original_requirements"] if item.get("experience_assessment")]
        if assessments:
            group["experience_assessment"] = assessments
        financial_assessments = [item.get("financial_assessment") for item in group["original_requirements"] if item.get("financial_assessment")]
        if financial_assessments:
            group["financial_assessment"] = financial_assessments
        review_actions = [item.get("review_action") for item in group["original_requirements"] if item.get("review_action")]
        if review_actions:
            group["review_actions"] = list(dict.fromkeys(review_actions))
        review_contexts = [item.get("review_context") for item in group["original_requirements"] if item.get("review_context")]
        if review_contexts:
            group["review_contexts"] = review_contexts
        group["source_pages"] = sorted({
            page for page in (
                [source.get("source_page") for source in known_sources] +
                [item.get("source_page") for item in group["original_requirements"]]
            ) if isinstance(page, int)
        })

    category_order = [
        "Eligibility & Company Documents",
        "Financial Requirements",
        "Experience & Past Performance",
        "Certifications & Authorizations",
        "Tender Declarations & Commitments",
    ]
    cards = grouped
    cards.sort(key=lambda card: (
        category_order.index(card["tender_category"])
        if card["tender_category"] in category_order else len(category_order),
        _match_normalize(card.get("requirement_title")),
    ))
    for card in cards:
        card["requirement_id"] = "group:" + hashlib.sha256(
            json.dumps({
                "tender_category": card["tender_category"],
                "key": _presentation_group_key(card),
                "requirement_ids": sorted(set(card["original_requirement_ids"])),
            }, sort_keys=True).encode("utf-8")
        ).hexdigest()

    excluded_summary = {
        "technical_compliance": technical_count,
        "informational": informational_count,
        "uncertain_not_card": uncertain_count,
    }
    return cards, excluded_summary


def _user_facing_category(requirement):
    text = _classification_text(requirement).lower()
    category = str(requirement.get("category", "") or "")
    if re.search(r"\b(turnover|net worth|financial|solvency|audited)\b", text):
        return "Financial Requirements"
    if re.search(r"\b(?:performance security|bid security|non[- ]?blacklisting|blacklist|debar|undertaking|signatory declaration|acceptance of tender|accept tender conditions|bidder commitment|delivery commitment|warranty commitment)\b", text):
        return "Tender Declarations & Commitments"
    if re.search(r"\b(experience|past performance|past supply|supply orders?|work orders?|purchase orders?|completed projects?|completion certificate|similar work|project value)\b", text):
        return "Experience & Past Performance"
    if re.search(r"\b(?:registration|incorporation|legal eligibility|eligibility declaration|company eligibility)\b", text):
        return "Eligibility & Company Documents"
    if re.search(r"\b(iso|certificat|authorization|authorisation|license|licence|dealer|oem|manufacturer)\b", text):
        return "Certifications & Authorizations"
    if re.search(r"\beligibility\b", text) or category in {"Eligibility Requirements", "Required Documents and Certificates", "EMD and Tender Fees", "Other Mandatory Bidder Conditions"}:
        return "Eligibility & Company Documents"
    if re.search(r"\b(declaration|undertaking|debar|blacklist|signatory|commitment|warranty|performance security|accept|deliver|supply|install)\b", text):
        return "Tender Declarations & Commitments"
    if category in {"Eligibility Requirements", "Required Documents and Certificates", "EMD and Tender Fees",
                    "Other Mandatory Bidder Conditions"}:
        return "Eligibility & Company Documents"
    return "Tender Declarations & Commitments"


def _presentation_group_key(requirement):
    text = _classification_text(requirement)
    category = _user_facing_category(requirement)
    value = _match_normalize(requirement.get("requirement_value"))
    applicability = _match_normalize(requirement.get("applicability_condition"))
    status = _match_normalize(requirement.get("requirement_status"))
    years = sorted(_financial_years(text))
    periods = "|".join(years)
    deadlines = "|".join(sorted(set(re.findall(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", text))))

    if re.search(r"\b(?:blacklist|debar)\b", text, re.I):
        canonical = "declaration_blacklisting"
    elif re.search(r"\bpan\b", text, re.I) and re.search(r"\bgst(?:in)?\b", text, re.I):
        canonical = "documents_pan_gst"
    elif re.search(r"\b(?:pan|gst(?:in)?)\b", text, re.I):
        canonical = "documents_pan_gst"
    elif re.search(r"\b(?:incorporation|registration certificate|certificate of incorporation|company registration proof|registration proof|proof of registration)\b", text, re.I):
        canonical = "documents_registration_proof"
    elif re.search(r"\b(?:authori[sz]ed signator(?:y|ies)|signatory details?)\b", text, re.I):
        canonical = "authorized_signatory_details"
    elif re.search(r"\b(?:turnover|audited|financial statements?|turnover statements?)\b", text, re.I) and category == "Financial Requirements":
        canonical = "financial_qualification"
    elif re.search(r"\bturnover\b", text, re.I):
        canonical = "financial_turnover"
    elif re.search(r"\bnet worth\b", text, re.I):
        canonical = "financial_net_worth"
    elif re.search(r"\bsimilar work|work order|past performance|experience\b", text, re.I):
        canonical = "experience_past_work"
    else:
        canonical = _match_normalize(requirement.get("requirement_title"))
        if not canonical:
            canonical = _match_normalize(requirement.get("description"))

    constraints = "|".join(re.findall(
        r"\b\d+(?:\.\d+)?\s*(?:crore|cr|lakh|lakhs|lac|million|billion|inr|rs|%)?\b",
        text,
        re.I,
    ))
    # Financial and experience requirements with different thresholds stay separate,
    # even when extraction left requirement_value empty.
    if category in {"Financial Requirements", "Experience & Past Performance"}:
        value = value or constraints
    return "|".join((category, canonical, value, periods, deadlines, applicability, status))


_FINANCIAL_AMOUNT_PATTERN = re.compile(
    r"(?:(?:\u20b9|\u00e2\u201a\u00b9))\s*\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|lac|million|billion)?|"
    r"(?:₹|\binr\b|\brs\.?\s*)\s*\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|lac|million|billion)?|"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakhs?|lac|million|billion)\b",
    re.I,
)


def _financial_group_details(requirement):
    text = _classification_text(requirement)
    if _user_facing_category(requirement) != "Financial Requirements":
        return None
    if re.search(r"\bnet worth\b", text, re.I):
        family = "net_worth"
    elif re.search(r"\b(turnover|audited|financial statements?|turnover statements?)\b", text, re.I):
        family = "turnover"
    else:
        return None
    thresholds = set()
    for match in _FINANCIAL_AMOUNT_PATTERN.finditer(text):
        amount = _money_value(match.group(0))
        if amount is not None:
            thresholds.add(amount)
    years = tuple(sorted(_financial_years(text)))
    duration = re.search(r"\b(last|past|preceding|previous)\s+(\d+|one|two|three|four|five)\s+(?:financial\s+)?years?\b", text, re.I)
    number_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    duration_period = ""
    if duration:
        amount = duration.group(2).lower()
        duration_period = f"{duration.group(1).lower()}:{number_words.get(amount, amount)}"
    method = "average" if re.search(r"\baverage\b", text, re.I) else (
        "cumulative" if re.search(r"\b(cumulative|aggregate total)\b", text, re.I) else (
            "each_year" if re.search(r"\b(each|every)\s+(?:financial\s+)?year\b", text, re.I) else ""
        )
    )
    return {
        "family": family,
        "thresholds": thresholds,
        "periods": years,
        "duration": duration_period,
        "method": method,
        "applicability": _match_normalize(requirement.get("applicability_condition")),
        "status": _match_normalize(requirement.get("requirement_status")),
    }


def _financial_groups_compatible(existing_requirements, requirement):
    new = _financial_group_details(requirement)
    old_details = [_financial_group_details(item) for item in existing_requirements]
    old_details = [item for item in old_details if item]
    if not new or not old_details:
        return False
    return all(old["family"] == new["family"] for old in old_details)


def _financial_subconditions(requirements):
    output = []
    for item in requirements:
        text = _classification_text(item)
        details = _financial_group_details(item) or {}
        thresholds = sorted(details.get("thresholds", set()))
        amount_text = next(iter(_FINANCIAL_AMOUNT_PATTERN.findall(text)), "")
        period = ", ".join(details.get("periods", ()))
        if not period and details.get("duration"):
            duration_kind, duration_count = details["duration"].split(":", 1)
            period = f"{duration_kind.title()} {duration_count} financial years"
        evidence = []
        if re.search(r"\b(audit|financial statement|audited account)\b", text, re.I):
            evidence.append("Audited statements")
        if re.search(r"\b(turnover statement|certified turnover)\b", text, re.I):
            evidence.append("Certified turnover statements")
        condition = {
            "family": details.get("family", "turnover"),
            "thresholds": set(thresholds),
            "periods": details.get("periods", ()),
            "duration": details.get("duration", ""),
            "method": details.get("method", ""),
            "applicability": details.get("applicability", ""),
            "status_key": details.get("status", ""),
        }
        existing = next((row for row in output if _financial_subconditions_compatible(row["_condition"], condition)), None)
        if existing:
            existing["_condition"]["thresholds"].update(condition["thresholds"])
            existing["requirement_ids"].append(item.get("requirement_id"))
            if item.get("source_page") is not None and item["source_page"] not in existing["source_pages"]:
                existing["source_pages"].append(item["source_page"])
            existing["evidence"] = list(dict.fromkeys(existing["evidence"] + evidence))
            if not existing["method"]:
                existing["method"] = details.get("method", "")
                existing["_condition"]["method"] = condition["method"]
            if not existing["threshold"]:
                existing["threshold"] = amount_text or (f"{thresholds[0]:g}" if thresholds else "")
            if not existing["period"]:
                existing["period"] = period
                existing["_condition"]["periods"] = condition["periods"]
                existing["_condition"]["duration"] = condition["duration"]
            existing["status"] = max((existing["status"], item.get("match_status", "")),
                                      key=lambda value: {"review_required": 4, "missing_information": 3, "document_needed": 2, "satisfied": 1}.get(value, 0))
        else:
            output.append({
            "requirement_ids": [item.get("requirement_id")],
            "source_pages": [item["source_page"]] if item.get("source_page") is not None else [],
            "threshold": amount_text or (f"{thresholds[0]:g}" if thresholds else ""),
            "period": period,
            "method": details.get("method", ""),
            "evidence": evidence,
            "status": item.get("match_status", ""),
            "_condition": condition,
        })
    for condition in output:
        condition["source_pages"] = sorted(condition["source_pages"])
        condition.pop("_condition", None)
    return output


def _financial_subconditions_compatible(left, right):
    for key in ("family", "periods", "duration", "method", "applicability"):
        if left[key] and right[key] and left[key] != right[key]:
            return False
    if left["periods"] and right["duration"] or right["periods"] and left["duration"]:
        return False
    return not (left["thresholds"] and right["thresholds"] and left["thresholds"] != right["thresholds"])


def _match_normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_phrase(text, phrase):
    normalized_text = f" {_match_normalize(text)} "
    normalized_phrase = _match_normalize(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in normalized_text


def _money_value(value):
    original = str(value or "")
    has_rupee_marker = bool(re.search(r"\u20b9|\u00e2[\u201a\u201e\u0082]\u00b9", original))
    original = re.sub(r"\u00e2[\u201a\u201e\u0082]\u00b9", "\u20b9", original)
    text = unicodedata.normalize("NFKC", original).casefold()
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(crore|cr|lakhs?|lac|thousand|million|billion|rs|inr)?", text)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    unit = match.group(2) or ""
    factors = {
        "crore": 10000000.0, "cr": 10000000.0,
        "lakh": 100000.0, "lac": 100000.0, "lakhs": 100000.0,
        "thousand": 1000.0, "million": 1000000.0,
        "billion": 1000000000.0, "rs": 1.0, "inr": 1.0,
    }
    if not unit and (re.search(r"\b(?:rs\.?|inr)\b", text, re.I) or has_rupee_marker):
        unit = "inr"
    if not unit:
        return None
    return amount * factors.get(unit, 1.0)


def _experience_years(value):
    text = str(value or "").lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years?|yrs?)", text)
    if match:
        return float(match.group(1))
    if re.fullmatch(r"\d+(?:\.\d+)?", text.strip()):
        return float(text.strip())
    return None


def _requirement_years(text):
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(?:\+?\s*)?(?:years?|yrs?)", str(text).lower())
    if not matches:
        return None
    return max(float(value) for value in matches)


def _financial_years(text):
    years = set()
    for first, second in re.findall(r"(20\d{2})\s*(?:[-/])\s*(20\d{2}|20\d{2}|(?:\d{2}))", str(text)):
        if second.isdigit() and len(second) == 2:
            second = f"{first[:2]}{second}"
        years.add(f"{first}-{second}")
    return years


def _financial_year_duration(text):
    match = re.search(r"\b(?:last|past|preceding|previous)\s+(\d+|one|two|three|four|five)\s+(?:financial\s+)?years?\b", str(text), re.I)
    if not match:
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    value = match.group(1).lower()
    return words.get(value, int(value) if value.isdigit() else None)


def _expanded_financial_years(text):
    years = _financial_years(text)
    if len(years) < 2 or not re.search(r"\b(?:to|through|until)\b", str(text), re.I):
        return sorted(years)
    starts = sorted(int(period[:4]) for period in years)
    return [f"{year}-{year + 1}" for year in range(starts[0], starts[-1] + 1)]


def _last_closed_financial_years(count):
    now = datetime.now(timezone.utc)
    current_fy_start = now.year if now.month >= 4 else now.year - 1
    last_closed_start = current_fy_start - 1
    first_start = last_closed_start - count + 1
    return [f"{year}-{year + 1}" for year in range(first_start, last_closed_start + 1)]


def _saved_turnover_by_year(profile):
    records = {}
    entries = (
        (profile.get("financial_year"), profile.get("annual_turnover")),
        (profile.get("turnover_current_year"), profile.get("turnover_current_amount")),
        (profile.get("turnover_previous_year"), profile.get("turnover_previous_amount")),
    )
    for year_text, amount_text in entries:
        if not year_text or not amount_text:
            continue
        amount = _money_value(amount_text)
        if amount is None:
            continue
        for year in _financial_years(year_text):
            records[year] = amount
    return records


def _stable_requirement_key(requirement):
    explicit = requirement.get("requirement_key") or requirement.get("topic_key")
    if explicit:
        return str(explicit)
    payload = "|".join(str(requirement.get(field, "") or "") for field in (
        "category", "requirement_title", "description", "requirement_value",
        "requirement_status", "applicability_condition", "source_page",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_evidence(requirement):
    sources = requirement.get("source_evidence")
    if isinstance(sources, list):
        return [dict(source) for source in sources if isinstance(source, dict)]
    evidence = requirement.get("evidence_text")
    if evidence:
        return [{"source_page": requirement.get("source_page"), "evidence_text": evidence}]
    return []


def _document_names(profile):
    documents = profile.get("supporting_documents")
    if not isinstance(documents, list):
        return []
    return [document.get("name", "") for document in documents if isinstance(document, dict)]


def _document_available(profile, terms):
    available_names = [_match_normalize(name) for name in _document_names(profile)]
    if not available_names:
        return False
    for term in terms:
        normalized = _match_normalize(term)
        if normalized and any(normalized in name or name in normalized for name in available_names):
            return True
    return False


def _base_match(requirement):
    confidence = requirement.get("confidence")
    validation_reasons = requirement.get("validation_reasons")
    validation_reasons = [str(reason) for reason in validation_reasons] if isinstance(validation_reasons, list) else []
    conflict = any("conflict" in reason.casefold() for reason in validation_reasons)
    evidence_warning = None
    if (requirement.get("validation_status") == "needs_review" and not conflict) or (
        isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence < 0.5
    ):
        evidence_warning = {
            "confidence": confidence,
            "message": "; ".join(validation_reasons) or "The source extraction is uncertain; verify the cited tender evidence before submission.",
            "source_page": requirement.get("source_page"),
            "evidence_text": requirement.get("evidence_text", ""),
        }
    return {
        "requirement_id": _stable_requirement_key(requirement),
        "category": requirement.get("category", ""),
        "requirement_title": requirement.get("requirement_title", ""),
        "description": requirement.get("description", ""),
        "requirement_value": requirement.get("requirement_value", ""),
        "mandatory": requirement.get("mandatory") is True or requirement.get("requirement_status") == "mandatory",
        "requirement_status": requirement.get("requirement_status", ""),
        "applicability_condition": requirement.get("applicability_condition", ""),
        "source_page": requirement.get("source_page"),
        "evidence_text": requirement.get("evidence_text", ""),
        "source_evidence": _source_evidence(requirement),
        "validation_status": requirement.get("validation_status", ""),
        "validation_reasons": validation_reasons,
        "confidence": confidence,
        "evidence_warning": evidence_warning,
        "matching_profile_fields": [],
        "match_status": "missing_information",
        "reason": "Enter the company information requested by the tender.",
        "additional_user_input_needed": True,
        "supporting_document_needed": False,
        "suggested_input_type": "confirmation",
        "editable_profile_fields": [],
        "review_action": None,
    }


def _finish_match(match, status, reason, fields=None, document_needed=False, input_type="confirmation"):
    match["match_status"] = status
    match["reason"] = reason
    match["matching_profile_fields"] = fields or []
    match["supporting_document_needed"] = document_needed
    match["additional_user_input_needed"] = status in {"missing_information", "document_needed", "review_required"}
    match["suggested_input_type"] = input_type
    match["completion_requires_profile_match"] = (
        status == "missing_information" and
        bool(set(fields or []).intersection(PROFILE_FIELDS) or "past_projects" in (fields or []))
    )
    return match


def match_tender_requirements(profile, analysis):
    requirements = analysis.get("requirements") if isinstance(analysis, dict) else None
    if not isinstance(requirements, list):
        return []

    results = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        match = _base_match(requirement)
        text = " ".join(str(requirement.get(field, "") or "") for field in (
            "requirement_title", "description", "requirement_value",
            "applicability_condition", "evidence_text",
        ))
        category = str(requirement.get("category", "") or "")
        status = str(requirement.get("requirement_status", "") or "")
        reasons = requirement.get("validation_reasons")

        reasons = reasons if isinstance(reasons, list) else []
        conflict_reasons = [str(reason) for reason in reasons if "conflict" in str(reason).casefold()]
        if conflict_reasons:
            source_page = requirement.get("source_page")
            source = requirement.get("evidence_text") or "No quotation was retained."
            reason = "The analysis found conflicting tender values. Compare the cited source "
            if source_page is not None:
                reason += f"on page {source_page} ({source}) with the other extracted clause, then confirm which value controls."
            else:
                reason += f"({source}) with the other extracted clause, then confirm which value controls."
            match["review_action"] = "Compare the conflicting source clauses and identify the controlling requirement."
            match["review_context"] = {"kind": "conflict", "source_page": source_page, "evidence_text": source, "conflict_reasons": conflict_reasons}
            results.append(_finish_match(match, "review_required", reason, input_type="text"))
            continue
        conditional_match = re.search(r"\b(unless|except|provided that|subject to|one of|and/or|consortium|joint venture)\b", text, re.I)
        if conditional_match or status == "conditional":
            condition = requirement.get("applicability_condition") or text
            results.append(_finish_match(
                match, "review_required",
                f"This requirement is conditional ({condition.strip()}); confirm whether the stated condition applies to this bidder.",
                input_type="confirmation",
            ))
            match["review_action"] = f"Confirm whether this condition applies: {condition.strip()}"
            match["review_context"] = {"kind": "condition", "source_page": requirement.get("source_page"), "evidence_text": requirement.get("evidence_text", ""), "condition": condition.strip()}
            continue

        if status in {"informational", "optional"} or category == "Tender Overview":
            results.append(_finish_match(
                match, "not_applicable",
                "This is informational or optional and does not establish a required company obligation.",
                input_type="confirmation",
            ))
            continue

        explicit_document = bool(re.search(
            r"\b(?:certificate|certification|document|copy|declaration|undertaking|license|licence|work order|completion|statement|audit report|proof|evidence)\b",
            text, re.I,
        ))
        document_terms = []
        document_aliases = {
            "pan": ["pan", "permanent account number"],
            "gst": ["gst", "gstin", "gst certificate"],
            "incorporation": ["incorporation", "registration certificate", "cin", "llpin"],
            "udyam": ["udyam", "msme"],
            "audited": ["audited financial", "audited statement", "audit report", "financial statement"],
            "turnover": ["turnover certificate", "turnover statement", "certified turnover"],
            "work order": ["work order"],
            "completion": ["completion certificate", "work completion"],
            "experience": ["experience certificate"],
        }
        for key, aliases in document_aliases.items():
            if any(_match_phrase(text, alias) for alias in aliases):
                document_terms.extend(aliases)

        # Match identifiers against saved General Information before treating them
        # as generic document requests. Explicit proof still needs uploaded evidence.
        identity_checks = (
            (r"\bpan\b", "pan_number", ["pan"]),
            (r"\b(?:gst|gstin)\b", "gst_number", ["gst"]),
            (r"\b(?:udyam|msme)\b", "udyam_msme", ["udyam", "msme"]),
            (r"\b(?:cin|llpin)\b", "cin_llpin", ["cin", "llpin"]),
            (r"\b(?:incorporation|legal entity|company|firm|business) registration\b|\b(?:incorporation|registration) (?:proof|certificate)\b|\b(?:proof|evidence|certificate) of (?:incorporation|registration)\b|\bregistration number\b", "registration_number", ["incorporation", "registration"]),
            (r"\b(?:registration|business|entity) type\b", "registration_type", []),
            (r"\blegal name\b", "legal_name", []),
            (r"\btrade name\b", "trade_name", []),
            (r"\bcompany type\b|\bentity type\b", "company_type", []),
        )
        identity_matches = [(field, aliases) for pattern, field, aliases in identity_checks
                            if re.search(pattern, text, re.I)]
        if identity_matches:
            is_registration = any(field == "registration_number" for field, _ in identity_matches)
            if is_registration and not any(field == "registration_type" for field, _ in identity_matches) and re.search(r"\b(?:entity|registration|business) type\b", text, re.I):
                identity_matches.append(("registration_type", []))
            if is_registration and re.search(r"\b(?:registered legal name|legal name|registered name)\b", text, re.I):
                identity_matches.append(("legal_name", []))
            saved_fields = [field for field, _ in identity_matches if profile.get(field)]
            missing_fields = [field for field, _ in identity_matches if not profile.get(field)]
            if is_registration and "registration_number" in missing_fields and profile.get("cin_llpin"):
                missing_fields.remove("registration_number")
                saved_fields.append("cin_llpin")
            if is_registration and "registration_type" in missing_fields and profile.get("company_type"):
                missing_fields.remove("registration_type")
                saved_fields.append("company_type")
            required_doc_groups = [(field, aliases) for field, aliases in identity_matches if aliases]
            missing_doc_fields = [field for field, aliases in required_doc_groups
                                  if not any(_document_available(profile, [alias]) for alias in aliases)]
            evidence_available = bool(required_doc_groups) and not missing_doc_fields
            requires_proof = explicit_document or bool(re.search(r"\b(?:proof|evidence|certificate|copy|document)\b", text, re.I))
            if missing_fields:
                missing_parts = [field.replace("_", " ") for field in missing_fields]
                if requires_proof:
                    document_labels = {"pan_number": "PAN certificate", "gst_number": "GST certificate", "udyam_msme": "Udyam/MSME certificate", "cin_llpin": "registration certificate", "registration_number": "registration proof"}
                    missing_parts.extend(document_labels.get(field, f"{field.replace('_', ' ')} document") for field in missing_doc_fields)
                results.append(_finish_match(
                    match, "missing_information",
                    "Add or upload the missing items: " + ", ".join(missing_parts) + ".",
                    missing_fields, document_needed=requires_proof and not evidence_available, input_type="text",
                ))
            elif requires_proof and not evidence_available:
                results.append(_finish_match(
                    match, "document_needed",
                    "The company identifiers are saved, but the required " + ", ".join({"pan_number": "PAN certificate", "gst_number": "GST certificate", "udyam_msme": "Udyam/MSME certificate", "cin_llpin": "registration certificate", "registration_number": "registration proof"}.get(field, f"{field.replace('_', ' ')} document") for field in missing_doc_fields) + " is missing.",
                    saved_fields + ["supporting_documents"], document_needed=True, input_type="document",
                ))
            else:
                results.append(_finish_match(
                    match, "satisfied",
                    "The matching company registration or tax details are saved" + (" with supporting evidence." if evidence_available else "."),
                    saved_fields + (["supporting_documents"] if evidence_available else []),
                    input_type="confirmation",
                ))
            continue

        certification_entries = profile.get("certification_entries") if isinstance(profile.get("certification_entries"), list) else []
        certification_match = False
        for entry in certification_entries:
            if not isinstance(entry, dict):
                continue
            entry_terms = [
                str(entry.get(field, "") or "").strip()
                for field in ("certification_name", "issuing_authority", "certificate_number")
            ]
            if any(term and _match_phrase(text, term) for term in entry_terms):
                certification_match = True
                match["_cert_entry"] = entry
                break

        if re.search(r"\b(?:certificat|licen[sc]e|qualification|iso)\b", text, re.I):
            if certification_match:
                entry = match.pop("_cert_entry")
                document_id = entry.get("supporting_document_id")
                document_ids = {document.get("id") for document in profile.get("supporting_documents", []) if isinstance(document, dict)}
                if document_id and document_id in document_ids:
                    results.append(_finish_match(
                        match, "satisfied",
                        "A matching saved certification and supporting document were found.",
                        ["certification_entries", "supporting_documents"], input_type="confirmation",
                    ))
                elif explicit_document:
                    results.append(_finish_match(
                        match, "document_needed",
                        "A matching certification record exists, but its supporting evidence is not uploaded.",
                        ["certification_entries", "supporting_documents"], document_needed=True,
                        input_type="document",
                    ))
                else:
                    results.append(_finish_match(
                        match, "satisfied",
                        "A matching saved certification was found; confirm that the current certificate remains valid.",
                        ["certification_entries"], input_type="confirmation",
                    ))
            else:
                suitable_document = _document_available(profile, document_terms or ["certificate"])
                if suitable_document:
                    results.append(_finish_match(
                        match, "satisfied",
                        "A suitable supporting document for this certification or qualification is available.",
                        ["supporting_documents"], input_type="confirmation",
                    ))
                else:
                    document_needed = explicit_document
                    results.append(_finish_match(
                        match,
                        "document_needed" if document_needed else "missing_information",
                        "No matching saved certification or qualification was found.",
                        ["certification_entries"], document_needed=document_needed,
                        input_type="document" if document_needed else "text",
                    ))
            continue

        if re.search(r"\b(?:authori[sz]ed signator(?:y|ies)|signatory details?)\b", text, re.I):
            signatory_name = profile.get("authorized_signatory_name") or profile.get("contact_name")
            signatory_role = profile.get("authorized_signatory_role") or profile.get("contact_role")
            required_signatory_fields = ["contact_name", "contact_role"]
            if re.search(r"\b(?:email|e-mail)\b", text, re.I):
                required_signatory_fields.append("contact_email")
            if re.search(r"\b(?:phone|telephone|mobile|contact number)\b", text, re.I):
                required_signatory_fields.append("contact_phone")
            if re.search(r"\b(?:legal company name|registered legal name|company name)\b", text, re.I):
                required_signatory_fields.append("legal_name")
            if re.search(r"\b(?:registered address|company address)\b", text, re.I):
                required_signatory_fields.append("address")
            values = {
                "contact_name": signatory_name,
                "contact_role": signatory_role,
                "contact_email": profile.get("contact_email"),
                "contact_phone": profile.get("contact_phone"),
            }
            missing_signatory_fields = [field for field in required_signatory_fields if not values.get(field)]
            if signatory_role and not re.search(r"\b(?:authori[sz]ed|director|partner|proprietor|signatory|power of attorney)\b", str(signatory_role), re.I):
                if "contact_role" not in missing_signatory_fields:
                    missing_signatory_fields.append("contact_role")
                match["editable_profile_fields"] = ["contact_role"]
            execution_required = bool(re.search(r"\b(?:signature|signed by|company seal|company stamp|date of signing|signing date)\b", text, re.I))
            if not missing_signatory_fields:
                results.append(_finish_match(
                    match,
                    "document_needed" if execution_required else "satisfied",
                    "Upload the tender-specific signed and dated execution document." if execution_required else "Saved contact details identify an authorised signatory.",
                    required_signatory_fields + (["supporting_documents"] if execution_required else []),
                    document_needed=execution_required, input_type="document" if execution_required else "confirmation",
                ))
            else:
                results.append(_finish_match(
                    match, "missing_information",
                    "Add the missing authorised signatory details: " + ", ".join(field.replace("_", " ") for field in missing_signatory_fields) + ".",
                    missing_signatory_fields, document_needed=execution_required, input_type="text",
                ))
            continue

        if re.search(r"\b(?:turnover|net worth|financial capacity|annual revenue)\b", text, re.I):
            profile_field = "net_worth" if _match_phrase(text, "net worth") else "annual_turnover"
            requirement_value = str(requirement.get("requirement_value") or "")
            required_money = _money_value(requirement_value) if requirement_value else None
            required_money = required_money if required_money is not None else _money_value(text)
            periods = _expanded_financial_years(text)
            required_duration = _financial_year_duration(text)
            by_year = _saved_turnover_by_year(profile)
            assessment_years = periods or (_last_closed_financial_years(required_duration) if required_duration else [])
            match["financial_assessment"] = {
                "family": profile_field,
                "threshold": required_money,
                "financial_years": assessment_years,
                "calculation_method": (_financial_group_details(requirement) or {}).get("method", "each year"),
                "available_values": {year: by_year[year] for year in assessment_years if year in by_year},
                "missing_years": [year for year in assessment_years if year not in by_year],
            }
            profile_fields = ["annual_turnover", "financial_year", "turnover_current_year", "turnover_current_amount", "turnover_previous_year", "turnover_previous_amount", "audited_financial_information"]
            if profile_field == "net_worth":
                financial_values = [_money_value(profile.get("net_worth"))] if profile.get("net_worth") else []
            else:
                if periods:
                    financial_values = [by_year[year] for year in periods] if all(year in by_year for year in periods) else []
                elif required_duration:
                    selected_years = _last_closed_financial_years(required_duration)
                    financial_values = [by_year[year] for year in selected_years] if all(year in by_year for year in selected_years) else []
                else:
                    current_value = profile.get("annual_turnover") or profile.get("turnover_current_amount")
                    financial_values = [_money_value(current_value)] if current_value else []
            financial_values = [value for value in financial_values if value is not None]
            document_requested = explicit_document or bool(re.search(r"\b(?:audited|auditor|certified turnover|turnover statement|financial statements?)\b", text, re.I))
            evidence_available = _document_available(profile, document_terms or ["audited statement", "financial statement", "turnover statement", "turnover certificate"])
            evidence_missing = document_requested and not evidence_available

            if not financial_values:
                results.append(_finish_match(
                    match, "missing_information",
                    "Turnover values for the required financial period are missing from General Information.",
                    profile_fields, document_needed=evidence_missing, input_type="number",
                ))
                continue
            if required_money is None:
                threshold_stated = bool(_FINANCIAL_AMOUNT_PATTERN.search(text))
                if evidence_missing:
                    results.append(_finish_match(
                        match, "document_needed", "Upload audited or certified financial evidence for this requirement.",
                        ["supporting_documents"], document_needed=True, input_type="document",
                    ))
                elif evidence_available and not threshold_stated:
                    results.append(_finish_match(
                        match, "satisfied", "Matching financial statement or turnover evidence is already uploaded.",
                        ["supporting_documents"], input_type="confirmation",
                    ))
                elif not threshold_stated:
                    results.append(_finish_match(
                        match, "missing_information", "Enter the financial year and turnover value requested by the tender.",
                        profile_fields, input_type="number",
                    ))
                else:
                    results.append(_finish_match(
                        match, "review_required",
                        "The tender states a financial amount, but its units could not be interpreted safely.",
                        profile_fields, input_type="confirmation",
                    ))
                continue
            comparison_value = (sum(financial_values) / len(financial_values)
                                if _financial_group_details(requirement) and _financial_group_details(requirement)["method"] == "average"
                                else min(financial_values))
            if comparison_value < required_money:
                results.append(_finish_match(
                    match, "missing_information",
                    "Saved turnover does not meet the stated threshold for the full required period; provide an accurate financial figure or calculation.",
                    profile_fields, document_needed=evidence_missing, input_type="number",
                ))
            elif evidence_missing:
                results.append(_finish_match(
                    match, "document_needed",
                    "Saved turnover meets the stated amount, but the requested audited or certified financial evidence is missing.",
                    ["supporting_documents"], document_needed=True, input_type="document",
                ))
            else:
                results.append(_finish_match(
                    match, "satisfied",
                    "Saved turnover for the required period meets the threshold and required evidence is available.",
                    profile_fields, input_type="confirmation",
                ))
            continue

        if re.search(r"\b(?:years in business|years? of experience|experience of \d+(?:\.\d+)? years?|prior work)\b", text, re.I):
            required_years = _requirement_years(text)
            saved_years = _experience_years(profile.get("years_experience"))
            if saved_years is None:
                results.append(_finish_match(
                    match, "missing_information",
                    "The company's years of experience are not saved.",
                    ["years_experience"], input_type="number",
                ))
            elif required_years is not None and saved_years >= required_years:
                results.append(_finish_match(
                    match, "satisfied",
                    "The saved years of experience meet the stated requirement.",
                    ["years_experience"], input_type="confirmation",
                ))
            elif required_years is not None:
                results.append(_finish_match(
                    match, "missing_information",
                    "The saved years of experience are below the stated threshold.",
                    ["years_experience"], input_type="number",
                ))
            else:
                results.append(_finish_match(
                    match, "review_required",
                    "The experience requirement cannot be interpreted as a reliable numerical threshold.",
                    ["years_experience"], input_type="confirmation",
                ))
            continue

        if re.search(r"\b(?:project|work order|similar work|past experience|completed work|past supply)\b", text, re.I):
            projects = profile.get("past_projects") if isinstance(profile.get("past_projects"), list) else []
            if not projects:
                results.append(_finish_match(
                    match, "missing_information",
                    "No reusable past project or work-order entries are saved.",
                    ["past_projects"], input_type="text",
                ))
                continue
            required_count_match = re.search(r"(?:minimum|min|at least)?\s*(\d+|one|two|three|four|five)\b.{0,50}\b(?:projects?|works?|orders?|contracts?)\b", text, re.I)
            count_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
            count_text = required_count_match.group(1).lower() if required_count_match else ""
            required_count = count_words.get(count_text, int(count_text) if count_text.isdigit() else None)
            qualifying_terms = [word for word in _match_normalize(text).split()
                                if len(word) > 3 and word not in {"similar", "projects", "project", "experience", "minimum", "years", "within", "last", "period", "supply", "works", "work", "required", "requirement", "company", "bidder", "tender", "provide", "contract", "completion", "certificate", "financial", "orders", "order", "least", "three", "four", "five", "during", "financials"}]
            required_periods = _expanded_financial_years(text)
            requested_duration = _financial_year_duration(text)
            if not required_periods and requested_duration and re.search(r"\bfinancial years?\b", text, re.I):
                required_periods = _last_closed_financial_years(requested_duration)
            duration_match = re.search(r"\b(?:last|past|within)\s+(\d+|one|two|three|four|five)\s+years?\b", text, re.I)
            duration_text = duration_match.group(1).lower() if duration_match else ""
            cutoff_count = count_words.get(duration_text, int(duration_text) if duration_text.isdigit() else None)
            cutoff_year = datetime.now(timezone.utc).year - cutoff_count if cutoff_count and not re.search(r"\bfinancial years?\b", text, re.I) else None
            matched_projects = []
            considered_projects = []
            for project in projects:
                if not isinstance(project, dict):
                    continue
                project_text = _match_normalize(" ".join(str(project.get(field, "") or "") for field in
                                                          ("project_name", "client_department", "work_description", "contract_value", "completion_date")))
                term_matches = not qualifying_terms or any(term in project_text for term in qualifying_terms)
                completion_year_match = re.match(r"(20\d{2})", str(project.get("completion_date") or ""))
                completion_year = int(completion_year_match.group(1)) if completion_year_match else None
                period_matches = not required_periods or bool(completion_year and any(
                    int(period[:4]) <= completion_year <= int(period[-4:]) for period in required_periods
                ))
                if cutoff_year:
                    period_matches = period_matches and bool(completion_year and completion_year >= cutoff_year)
                if term_matches and period_matches:
                    matched_projects.append(project)
                considered_projects.append({
                    "project_name": project.get("project_name") or "Unnamed project",
                    "completion_date": project.get("completion_date") or "Date not recorded",
                    "matched": term_matches and period_matches,
                    "criteria_matched": ["description or client matches tender terms"] if term_matches else [],
                    "criteria_missing": (["similarity terms: " + ", ".join(qualifying_terms)] if not term_matches else []) + (["required completion period"] if not period_matches else []),
                })
            match["experience_assessment"] = [{
                "required_count": required_count,
                "required_periods": required_periods,
                "similarity_terms": qualifying_terms,
                "projects_considered": considered_projects,
                "matched_count": len(matched_projects),
            }]
            if required_count is not None:
                if len(matched_projects) >= required_count:
                    linked_documents = {entry.get("supporting_document_id") for entry in matched_projects}
                    available_ids = {doc.get("id") for doc in profile.get("supporting_documents", []) if isinstance(doc, dict)}
                    if explicit_document and not any(document_id in available_ids for document_id in linked_documents if document_id):
                        results.append(_finish_match(
                            match, "document_needed",
                            "Saved projects meet the experience threshold, but supporting work evidence is missing.",
                            ["past_projects", "supporting_documents"], document_needed=True, input_type="document",
                        ))
                    else:
                        results.append(_finish_match(
                            match, "satisfied",
                            "Saved past projects match the stated count, type, and qualifying period.",
                            ["past_projects"], input_type="confirmation",
                        ))
                else:
                    results.append(_finish_match(
                        match, "missing_information",
                        "There are fewer saved past project entries than the stated count.",
                        ["past_projects"], input_type="text",
                    ))
                continue
            if matched_projects:
                results.append(_finish_match(
                    match, "satisfied",
                    "A saved past project matches the tender's experience description.",
                    ["past_projects"], input_type="confirmation",
                ))
            elif qualifying_terms:
                results.append(_finish_match(
                    match, "missing_information",
                    "Saved projects do not describe the required type of supply or work.",
                    ["past_projects"], input_type="text",
                ))
            else:
                results.append(_finish_match(
                    match, "missing_information",
                    "Add project details and qualifying periods needed to match this experience requirement.",
                    ["past_projects"], input_type="text",
                ))
            continue

        if document_terms:
            if _document_available(profile, document_terms):
                results.append(_finish_match(
                    match, "satisfied",
                    "A suitable supporting company document is available.",
                    ["supporting_documents"], input_type="confirmation",
                ))
            else:
                missing_field = {
                    "pan": "pan_number", "gst": "gst_number",
                    "incorporation": "registration_number", "udyam": "udyam_msme",
                    "audited": "audited_financial_information", "turnover": "annual_turnover",
                }.get(next((key for key, aliases in document_aliases.items()
                            if any(_match_phrase(text, alias) for alias in aliases)), ""))
                results.append(_finish_match(
                    match,
                    "document_needed" if explicit_document else "missing_information",
                    "No suitable supporting company document was found for this requirement.",
                    [missing_field] if missing_field else ["supporting_documents"],
                    document_needed=explicit_document,
                    input_type="document" if explicit_document else "text",
                ))
            continue

        capability_fields = (
                ("services", "services"),
                ("service", "services"),
                ("core business", "core_business_activities"),
                ("business activity", "core_business_activities"),
                ("industry", "industries_sectors_served"),
                ("sector", "industries_sectors_served"),
                ("product category", "product_categories"),
                ("employee", "employee_count"),
        )
        for phrase, field in capability_fields:
                if _match_phrase(text, phrase):
                    value = profile.get(field)
                    if value:
                        normalized_value = _match_normalize(value)
                        requirement_words = set(_match_normalize(text).split())
                        overlap = bool(normalized_value and set(normalized_value.split()).intersection(requirement_words))
                        if overlap:
                            results.append(_finish_match(
                                match, "satisfied",
                                "Saved capability information is relevant to the requirement.",
                                [field], input_type="confirmation",
                            ))
                        else:
                            results.append(_finish_match(
                                match, "missing_information",
                                f"Saved {field.replace('_', ' ')} does not identify the capability named in the tender; update the reusable profile details.",
                                [field], input_type="text",
                            ))
                    else:
                        results.append(_finish_match(
                            match, "missing_information",
                            "The relevant capability or company information is not saved.",
                            [field], input_type="text",
                        ))
                    break
        else:
            declaration = bool(re.search(r"\b(?:declaration|undertaking|blacklist|debar|signatory|commitment|accept|agree|warranty|performance security|deliver|supply|install)\b", text, re.I))
            action_clause = bool(re.search(r"\b(?:bidder|tenderer|company|supplier)\s+(?:shall|must|will|is required to)\b", text, re.I))
            if declaration or action_clause:
                commitment = "; ".join(str(requirement.get(field) or "").strip() for field in ("requirement_title", "description", "requirement_value") if str(requirement.get(field) or "").strip()) or "the stated tender commitment"
                results.append(_finish_match(
                    match, "missing_information",
                    f"Confirm whether the company accepts this commitment: {commitment}",
                    input_type="confirmation",
                ))
            elif category in {"Eligibility Requirements", "Required Documents and Certificates", "Financial Requirements", "Experience Requirements", "Other Mandatory Bidder Conditions"}:
                results.append(_finish_match(
                    match, "missing_information",
                    "The tender requires company information that is not available in the saved profile.",
                    input_type="text",
                ))
            else:
                results.append(_finish_match(
                    match, "not_applicable",
                    "No company action is established by this requirement.",
                    input_type="confirmation",
                ))
        continue

    return results
