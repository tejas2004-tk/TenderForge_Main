import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from threading import Lock

from bson import ObjectId
from flask import current_app, g, jsonify, request
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_database

_INDEX_LOCK = Lock()
_INDEXES_READY = False
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_LENGTH = timedelta(days=30)
SHORT_SESSION_LENGTH = timedelta(hours=12)


class AuthError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _collections():
    """Get auth collections and create their safe, repeatable indexes once."""
    global _INDEXES_READY
    database = get_database(current_app)
    if not _INDEXES_READY:
        with _INDEX_LOCK:
            if not _INDEXES_READY:
                database.users.create_index(
                    [("email", ASCENDING)], unique=True, name="unique_normalized_email"
                )
                database.auth_sessions.create_index(
                    [("token_hash", ASCENDING)], unique=True, name="unique_session_token"
                )
                database.auth_sessions.create_index(
                    [("expires_at", ASCENDING)], expireAfterSeconds=0, name="expire_sessions"
                )
                _INDEXES_READY = True
    return database.users, database.auth_sessions


def _public_user(user):
    return {
        "id": str(user["_id"]),
        "full_name": user["full_name"],
        "company_name": user["company_name"],
        "email": user["email"],
    }


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_cookie(response, token, max_age=None):
    response.set_cookie(
        current_app.config["SESSION_COOKIE_NAME"],
        token,
        max_age=max_age,
        httponly=current_app.config["SESSION_COOKIE_HTTPONLY"],
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
        path="/",
    )
    return response


def _clear_session_cookie(response):
    response.delete_cookie(
        current_app.config["SESSION_COOKIE_NAME"],
        httponly=current_app.config["SESSION_COOKIE_HTTPONLY"],
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        samesite=current_app.config["SESSION_COOKIE_SAMESITE"],
        path="/",
    )
    return response


def _current_user():
    token = request.cookies.get(current_app.config["SESSION_COOKIE_NAME"])
    if not token:
        return None
    try:
        users, sessions = _collections()
        stored_session = sessions.find_one({
            "token_hash": _token_hash(token),
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        })
        if not stored_session:
            return None
        user_id = stored_session.get("user_id")
        if not isinstance(user_id, ObjectId):
            return None
        return users.find_one({"_id": user_id})
    except PyMongoError as error:
        raise AuthError("Authentication service is temporarily unavailable.", 503) from error


def require_auth(handler):
    """Protect a Flask route and expose its user as g.auth_user."""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            user = _current_user()
        except AuthError as error:
            return jsonify({"message": str(error)}), error.status_code
        if not user:
            return jsonify({"message": "Please log in to continue."}), 401
        g.auth_user = user
        return handler(*args, **kwargs)
    return wrapped


def register_user(data):
    if not isinstance(data, dict):
        raise AuthError("Please send your registration details as JSON.")

    full_name = data.get("full_name", "").strip() if isinstance(data.get("full_name"), str) else ""
    company_name = data.get("company_name", "").strip() if isinstance(data.get("company_name"), str) else ""
    email = data.get("email", "").strip().lower() if isinstance(data.get("email"), str) else ""
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    confirm_password = data.get("confirm_password") if isinstance(data.get("confirm_password"), str) else ""

    if not all((full_name, company_name, email, password, confirm_password)):
        raise AuthError("Please complete all required fields.")
    if len(full_name) > 120 or len(company_name) > 160:
        raise AuthError("Name and company fields must be shorter than 160 characters.")
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise AuthError("Enter a valid email address.")
    if len(password) < 8 or len(password) > 128:
        raise AuthError("Password must be between 8 and 128 characters.")
    if password != confirm_password:
        raise AuthError("Passwords do not match.")

    users, _ = _collections()
    if users.find_one({"email": email}, {"_id": 1}):
        raise AuthError("An account with this email already exists.", 409)
    user = {
        "full_name": full_name,
        "company_name": company_name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now(timezone.utc),
    }
    try:
        result = users.insert_one(user)
    except DuplicateKeyError as error:
        raise AuthError("An account with this email already exists.", 409) from error
    except PyMongoError as error:
        raise AuthError("Could not create your account. Please try again.", 503) from error
    user["_id"] = result.inserted_id
    return _public_user(user)


def login_user(data):
    if not isinstance(data, dict):
        raise AuthError("Please send your login details as JSON.")
    email = data.get("email", "").strip().lower() if isinstance(data.get("email"), str) else ""
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    if not email or not password:
        raise AuthError("Enter your email and password.")
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise AuthError("Enter a valid email address.")

    users, sessions = _collections()
    user = users.find_one({"email": email})
    if not user or not check_password_hash(user["password_hash"], password):
        raise AuthError("Email or password is incorrect.", 401)

    remember = data.get("remember_me") is True
    now = datetime.now(timezone.utc)
    lifetime = SESSION_LENGTH if remember else SHORT_SESSION_LENGTH
    token = secrets.token_urlsafe(32)
    sessions.insert_one({
        "token_hash": _token_hash(token),
        "user_id": user["_id"],
        "created_at": now,
        "expires_at": now + lifetime,
    })
    return token, _public_user(user), int(lifetime.total_seconds()) if remember else None


def logout_user():
    token = request.cookies.get(current_app.config["SESSION_COOKIE_NAME"])
    if token:
        _, sessions = _collections()
        sessions.delete_one({"token_hash": _token_hash(token)})


def database_error_response(error):
    if isinstance(error, AuthError):
        return jsonify({"message": str(error)}), error.status_code
    return jsonify({"message": "Authentication service is temporarily unavailable."}), 503
