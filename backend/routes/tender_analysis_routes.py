from flask import Blueprint, g, jsonify, request
from pymongo.errors import PyMongoError

from services.auth_service import require_auth
from services.tender_analysis_service import get_analysis, resume_analysis, start_analysis, start_fresh_analysis
from services.tender_service import TenderError

analysis_bp = Blueprint("tender_analysis", __name__)


@analysis_bp.get("/<tender_id>/analysis")
@require_auth
def analysis_status(tender_id):
    try:
        return jsonify({"analysis": get_analysis(g.auth_user, tender_id)}), 200
    except (TenderError, PyMongoError) as error:
        return _error(error)


@analysis_bp.post("/<tender_id>/analysis")
@require_auth
def analyze_tender(tender_id):
    try:
        analysis, started = start_analysis(g.auth_user, tender_id)
        return jsonify({"analysis": analysis, "started": started}), 202 if analysis.get("status") == "processing" else 200
    except (TenderError, PyMongoError) as error:
        return _error(error)


@analysis_bp.post("/<tender_id>/analysis/resume")
@require_auth
def continue_analysis(tender_id):
    try:
        analysis = resume_analysis(g.auth_user, tender_id)
        return jsonify({"analysis": analysis}), 202 if analysis.get("status") == "processing" else 200
    except (TenderError, PyMongoError) as error:
        return _error(error)


@analysis_bp.post("/<tender_id>/analysis/fresh")
@require_auth
def reanalyze_tender(tender_id):
    try:
        request_id = request.headers.get("X-Run-Request-Id", "").strip()[:100] or None
        analysis, started = start_fresh_analysis(g.auth_user, tender_id, request_id=request_id)
        return jsonify({"analysis": analysis, "started": started}), 202 if analysis.get("status") == "processing" else 200
    except (TenderError, PyMongoError) as error:
        return _error(error)


def _error(error):
    if isinstance(error, TenderError):
        return jsonify({"message": str(error)}), error.status_code
    return jsonify({"message": "Could not complete tender analysis. Please try again."}), 503
