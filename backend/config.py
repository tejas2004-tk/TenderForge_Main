import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_DOTENV_PATH = Path(__file__).resolve().parent / ".env"
_DOTENV_VALUES = dotenv_values(_DOTENV_PATH)
# Keep dotenv's normal precedence for the rest of the application. In local
# development only, the model in backend/.env is authoritative so a stale
# inherited GEMINI_MODEL cannot silently redirect connection checks.
load_dotenv(_DOTENV_PATH)


def _effective_gemini_model(dotenv_model, environment=None, local_development=False):
    environment = os.environ if environment is None else environment
    if local_development and dotenv_model:
        return dotenv_model
    return environment.get("GEMINI_MODEL") or dotenv_model or "gemini-3.8-flash"


class Config:
    MONGODB_URI = os.getenv("MONGODB_URI", "")
    MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "tenderforge")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    TENDERFORGE_ENV = os.getenv("TENDERFORGE_ENV", "").lower()
    DEBUG = (
        False if TENDERFORGE_ENV in {"production", "prod"}
        else os.getenv("FLASK_DEBUG", str(_DOTENV_VALUES.get("FLASK_DEBUG", "false"))).lower() == "true"
    )
    LOCAL_DEVELOPMENT = TENDERFORGE_ENV == "development" or (not TENDERFORGE_ENV and DEBUG)
    GEMINI_MODEL = _effective_gemini_model(_DOTENV_VALUES.get("GEMINI_MODEL"), local_development=LOCAL_DEVELOPMENT)
    GEMINI_CONNECTION_TIMEOUT_MS = int(os.getenv("GEMINI_CONNECTION_TIMEOUT_MS", "90000"))
    GEMINI_CONNECTION_RETRY_ATTEMPTS = int(os.getenv("GEMINI_CONNECTION_RETRY_ATTEMPTS", "3"))
    GEMINI_CONNECTION_RETRY_BASE_SECONDS = float(os.getenv("GEMINI_CONNECTION_RETRY_BASE_SECONDS", "1"))
    GEMINI_CONNECTION_RETRY_MAX_SECONDS = float(os.getenv("GEMINI_CONNECTION_RETRY_MAX_SECONDS", "30"))
    FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
    PORT = int(os.getenv("PORT", "5000"))
    SESSION_COOKIE_SECURE = os.getenv("COOKIE_SECURE", str(not DEBUG)).lower() == "true"
    SESSION_COOKIE_NAME = os.getenv(
        "SESSION_COOKIE_NAME",
        "__Host-tenderforge_session" if SESSION_COOKIE_SECURE else "tenderforge_session",
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    MAX_TENDER_SIZE_MB = int(os.getenv("MAX_TENDER_SIZE_MB", "25"))
    ANALYSIS_MAX_INPUT_TOKENS = int(os.getenv("ANALYSIS_MAX_INPUT_TOKENS", "6000"))
    ANALYSIS_MAX_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_MAX_OUTPUT_TOKENS", "4000"))
    ANALYSIS_MAX_TPM_TOKENS = int(os.getenv("ANALYSIS_MAX_TPM_TOKENS", "8000"))
    ANALYSIS_REQUEST_INTERVAL_SECONDS = float(os.getenv("ANALYSIS_REQUEST_INTERVAL_SECONDS", "2"))
    ANALYSIS_RETRY_ATTEMPTS = int(os.getenv("ANALYSIS_RETRY_ATTEMPTS", "3"))
    ANALYSIS_RETRY_BASE_SECONDS = float(os.getenv("ANALYSIS_RETRY_BASE_SECONDS", "2"))
    ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS", "60"))
    ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS", "86400"))
    ANALYSIS_LEASE_SECONDS = int(os.getenv("ANALYSIS_LEASE_SECONDS", "300"))
    ANALYSIS_VERSION = os.getenv("ANALYSIS_VERSION", "6")
    ANALYSIS_PROMPT_VERSION = os.getenv("ANALYSIS_PROMPT_VERSION", "5")
