from flask import Blueprint, g, jsonify, request, send_from_directory
from pymongo.errors import PyMongoError

from services.auth_service import require_auth
from services.company_profile_service import (
    ProfileError,
    match_tender_requirements,
    delete_supporting_document,
    delete_tender_response,
    delete_tender_response_document,
    find_tender_response_document,
    get_company_profile,
    get_tender_responses,
    save_company_profile,
    save_supporting_documents,
    save_tender_response,
    save_tender_response_documents,
    find_supporting_document,
)
from services.tender_analysis_service import get_analysis
from services.tender_service import TenderError

company_profile_bp = Blueprint("company_profile", __name__)


@company_profile_bp.get("")
@require_auth
def get_profile():
    try:
        return jsonify({"profile": get_company_profile(g.auth_user)}), 200
    except (ProfileError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.get("/tenders/<tender_id>/requirements-match")
@require_auth
def get_requirements_match(tender_id):
    try:
        profile = get_company_profile(g.auth_user)
        analysis = get_analysis(g.auth_user, tender_id)
        matches = match_tender_requirements(profile, analysis)
        return jsonify({
            "tender_id": tender_id,
            "analysis_status": analysis.get("status", "not_started"),
            "matches": matches,
            "summary": _match_summary(matches),
        }), 200
    except (ProfileError, TenderError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.get("/tenders/<tender_id>/responses")
@require_auth
def get_responses(tender_id):
    try:
        return jsonify(get_tender_responses(g.auth_user, tender_id)), 200
    except (ProfileError, TenderError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.post("/tenders/<tender_id>/responses")
@require_auth
def update_response(tender_id):
    try:
        return jsonify(save_tender_response(
            g.auth_user, tender_id, request.get_json(silent=True),
        )), 200
    except (ProfileError, TenderError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.delete("/tenders/<tender_id>/responses/<requirement_id>")
@require_auth
def remove_response(tender_id, requirement_id):
    try:
        return jsonify(delete_tender_response(g.auth_user, tender_id, requirement_id)), 200
    except (ProfileError, TenderError, PyMongoError) as error:
        return _error_response(error)


@company_profile_bp.post("/tenders/<tender_id>/responses/<requirement_id>/documents")
@require_auth
def upload_response_document(tender_id, requirement_id):
    try:
        result = save_tender_response_documents(
            g.auth_user,
            tender_id,
            requirement_id,
            request.files.getlist("files"),
        )
        return jsonify(result), 201
    except (ProfileError, TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


@company_profile_bp.get("/tenders/<tender_id>/responses/<requirement_id>/documents/<document_id>")
@require_auth
def download_response_document(tender_id, requirement_id, document_id):
    try:
        document = find_tender_response_document(
            g.auth_user, tender_id, requirement_id, document_id,
        )
        return send_from_directory(
            document["directory"],
            document["storage_name"],
            as_attachment=True,
            download_name=document["name"],
        )
    except (ProfileError, TenderError, PyMongoError, OSError) as error:
        return _error_response(error)


@company_profile_bp.delete("/tenders/<tender_id>/responses/<requirement_id>/documents/<document_id>")
@require_auth
def remove_response_document(tender_id, requirement_id, document_id):
    try:
        return jsonify(delete_tender_response_document(
            g.auth_user, tender_id, requirement_id, document_id,
        )), 200
    except (ProfileError, TenderError, PyMongoError, OSError) as error:
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


def _match_summary(matches):
    statuses = ("satisfied", "missing_information", "document_needed", "review_required", "not_applicable")
    return {
        "total": len(matches),
        **{status: sum(item.get("match_status") == status for item in matches) for status in statuses},
    }


def _error_response(error):
    if isinstance(error, ProfileError):
        return jsonify({"message": str(error)}), error.status_code
    if isinstance(error, TenderError):
        return jsonify({"message": str(error)}), error.status_code
    return jsonify({"message": "Could not update the company profile. Please try again."}), 503
