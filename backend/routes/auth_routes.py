from flask import Blueprint, g, jsonify, request
from pymongo.errors import PyMongoError

from services.auth_service import (
    AuthError,
    _clear_session_cookie,
    _session_cookie,
    database_error_response,
    login_user,
    logout_user,
    require_auth,
    register_user,
)

auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/register")
def register():
    try:
        user = register_user(request.get_json(silent=True))
        return jsonify({"message": "Account created successfully. Please log in.", "user": user}), 201
    except (AuthError, PyMongoError) as error:
        return database_error_response(error)


@auth_bp.post("/login")
def login():
    try:
        token, user, max_age = login_user(request.get_json(silent=True))
        response = jsonify({"message": "Logged in successfully.", "user": user})
        return _session_cookie(response, token, max_age)
    except (AuthError, PyMongoError) as error:
        return database_error_response(error)


@auth_bp.post("/logout")
def logout():
    try:
        logout_user()
        return _clear_session_cookie(jsonify({"message": "Logged out successfully."}))
    except (AuthError, PyMongoError) as error:
        response, status_code = database_error_response(error)
        return _clear_session_cookie(response), status_code


@auth_bp.get("/me")
@require_auth
def me():
    return jsonify({"user": {
        "id": str(g.auth_user["_id"]),
        "full_name": g.auth_user["full_name"],
        "company_name": g.auth_user["company_name"],
        "email": g.auth_user["email"],
    }}), 200
