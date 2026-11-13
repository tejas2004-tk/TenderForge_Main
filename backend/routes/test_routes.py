from flask import Blueprint, current_app, jsonify
from pymongo.errors import PyMongoError

from database import get_database
from services.groq_service import GroqServiceError, test_groq_connection

test_bp = Blueprint("tests", __name__)


@test_bp.get("/test-db")
def test_database():
    try:
        get_database(current_app).command("ping")
        return jsonify({"status": "connected", "message": "MongoDB connection successful."}), 200
    except (PyMongoError, RuntimeError):
        return jsonify({"status": "error", "message": "MongoDB connection failed. Check backend configuration and Atlas network access."}), 503


@test_bp.get("/test-groq")
def test_groq():
    try:
        answer = test_groq_connection(current_app.config["GROQ_API_KEY"], current_app.config["GROQ_MODEL"])
        return jsonify({"status": "connected", "message": answer}), 200
    except GroqServiceError as error:
        return jsonify({"status": "error", "message": str(error)}), error.status_code
