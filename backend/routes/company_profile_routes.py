from flask import Blueprint, g, jsonify, request, send_from_directory
from pymongo.errors import PyMongoError

from services.auth_service import require_auth
from services.company_profile_service import (
    ProfileError,
    delete_supporting_document,
    get_company_profile,
    save_company_profile,
    save_supporting_documents,
    find_supporting_document,
)

company_profile_bp = Blueprint("company_profile", __name__)


@company_profile_bp.get("")
@require_auth
def get_profile():
    try:
        return jsonify({"profile": get_company_profile(g.auth_user)}), 200
    except (ProfileError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.put("")
@require_auth
def update_profile():
    try:
        profile = save_company_profile(g.auth_user, request.get_json(silent=True))
        return jsonify({"message": "Company profile saved.", "profile": profile}), 200
    except (ProfileError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.post("/documents")
@require_auth
def upload_documents():
    try:
        documents = save_supporting_documents(g.auth_user, request.files.getlist("files"))
        return jsonify({"documents": documents}), 201
    except (ProfileError, PyMongoError, OSError) as error:
        return _error_response(error)


@company_profile_bp.get("/documents/<document_id>")
@require_auth
def download_document(document_id):
    try:
        document = find_supporting_document(g.auth_user, document_id)
        return send_from_directory(
            document["directory"],
            document["storage_name"],
            as_attachment=True,
            download_name=document["name"],
        )
    except (ProfileError, PyMongoError, OSError) as error:
        return _error_response(error)


@company_profile_bp.delete("/documents/<document_id>")
@require_auth
def remove_document(document_id):
    try:
        delete_supporting_document(g.auth_user, document_id)
        return jsonify({"message": "Supporting document removed."}), 200
    except (ProfileError, PyMongoError, OSError) as error:
        return _error_response(error)


def _error_response(error):
    if isinstance(error, ProfileError):
        return jsonify({"message": str(error)}), error.status_code
    return jsonify({"message": "Could not update the company profile. Please try again."}), 503
