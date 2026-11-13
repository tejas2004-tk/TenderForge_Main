from io import BytesIO
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_file
from pymongo.errors import DuplicateKeyError, PyMongoError

from services.auth_service import require_auth
from services.pdf_processing_service import (
    get_extraction_status,
    list_extraction_summaries,
    process_tender,
)
from services.tender_service import (
    TenderError,
    delete_tender,
    download_tender,
    list_tenders,
    upload_tender,
)

tender_bp = Blueprint("tenders", __name__)


@tender_bp.post("")
@require_auth
def create_tender():
    try:
        tender, duplicate = upload_tender(
            g.auth_user,
            request.files.get("file"),
            current_app.config["MAX_TENDER_SIZE_MB"],
        )
        message = "This PDF is already in My Tenders." if duplicate else "Tender uploaded successfully."
        return jsonify({"message": message, "tender": tender, "duplicate": duplicate}), 200 if duplicate else 201
    except (TenderError, DuplicateKeyError, PyMongoError, OSError) as error:
        return _error_response(error)


@tender_bp.get("")
@require_auth
def get_tenders():
    try:
        tenders = list_tenders(g.auth_user)
        extraction_by_tender = list_extraction_summaries(g.auth_user)
        for tender in tenders:
            tender["extraction"] = extraction_by_tender.get(tender["id"], {
                "status": "not_processed",
                "page_count": 0,
                "extracted_pages": 0,
                "total_chunks": 0,
                "processing_started_at": None,
                "processing_finished_at": None,
                "error_message": None,
            })
        return jsonify({
            "tenders": tenders,
            "max_upload_size_mb": current_app.config["MAX_TENDER_SIZE_MB"],
        }), 200
    except (TenderError, PyMongoError) as error:
        return _error_response(error)


@tender_bp.post("/<tender_id>/process")
@require_auth
def start_pdf_processing(tender_id):
    try:
        extraction = process_tender(g.auth_user, tender_id)
        status_code = 202 if extraction.get("status") == "processing" else 200
        return jsonify({"extraction": extraction}), status_code
    except (TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


@tender_bp.get("/<tender_id>/extraction")
@require_auth
def extraction_status(tender_id):
    try:
        return jsonify({"extraction": get_extraction_status(g.auth_user, tender_id)}), 200
    except (TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


@tender_bp.get("/<tender_id>/download")
@require_auth
def get_tender_download(tender_id):
    try:
        tender = download_tender(g.auth_user, tender_id)
        file_path = Path(tender["directory"]) / tender["storage_name"]
        return send_file(
            BytesIO(file_path.read_bytes()),
            as_attachment=True,
            download_name=tender["filename"],
            mimetype="application/pdf",
        )
    except (TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


@tender_bp.delete("/<tender_id>")
@require_auth
def remove_tender(tender_id):
    try:
        delete_tender(g.auth_user, tender_id)
        return jsonify({"message": "Tender deleted."}), 200
    except (TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


def _error_response(error):
    if isinstance(error, TenderError):
        return jsonify({"message": str(error)}), error.status_code
    if isinstance(error, DuplicateKeyError):
        return jsonify({"message": "This PDF is already in My Tenders.", "duplicate": True}), 200
    return jsonify({"message": "Could not complete the tender request. Please try again."}), 503
