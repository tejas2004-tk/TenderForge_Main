from flask import Blueprint, current_app, jsonify
from pymongo.errors import PyMongoError

from database import get_database
from services.gemini_service import GeminiServiceError, test_gemini_connection

test_bp = Blueprint("tests", __name__)


@test_bp.get("/test-db")
def test_database():
    try:
        get_database(current_app).command("ping")
        return jsonify({"status": "connected", "message": "MongoDB connection successful."}), 200
    except (PyMongoError, RuntimeError):
        return jsonify({"status": "error", "message": "MongoDB connection failed. Check backend configuration and Atlas network access."}), 503


@test_bp.get("/test-gemini")
def test_gemini():
    model = current_app.config["GEMINI_MODEL"]
    try:
        answer = test_gemini_connection(
            current_app.config["GEMINI_API_KEY"], model, current_app.config,
        )
        return jsonify({"status": "connected", "model": model, "message": answer}), 200
    except GeminiServiceError as error:
        return jsonify({"status": "error", "model": model, "message": str(error)}), error.status_code
