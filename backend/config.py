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


def _provider_order(value):
    value = "groq,openrouter,gemini" if value is None else value
    providers = [item.strip().casefold() for item in value.split(",") if item.strip()]
    allowed = {"groq", "openrouter", "gemini"}
    if not providers or any(item not in allowed for item in providers) or len(set(providers)) != len(providers):
        raise ValueError("ANALYSIS_PROVIDER_ORDER must be a unique comma-separated list of groq,openrouter,gemini.")
    return providers


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
    ANALYSIS_PROVIDER_ORDER = _provider_order(os.getenv("ANALYSIS_PROVIDER_ORDER", "groq,openrouter,gemini"))
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b")
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
    OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "TenderForge")
    ANALYSIS_PROVIDER_TIMEOUT_SECONDS = float(os.getenv("ANALYSIS_PROVIDER_TIMEOUT_SECONDS", "30"))
    ANALYSIS_PROVIDER_CONNECT_TIMEOUT_SECONDS = float(os.getenv("ANALYSIS_PROVIDER_CONNECT_TIMEOUT_SECONDS", "10"))
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
    ANALYSIS_BATCH_TARGET_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_BATCH_TARGET_OUTPUT_TOKENS", "1200"))
    ANALYSIS_MIN_SEGMENT_CHARS = int(os.getenv("ANALYSIS_MIN_SEGMENT_CHARS", "120"))
    ANALYSIS_MAX_SPLIT_DEPTH = int(os.getenv("ANALYSIS_MAX_SPLIT_DEPTH", "3"))
    ANALYSIS_MAX_CONCURRENT_REQUESTS = int(os.getenv("ANALYSIS_MAX_CONCURRENT_REQUESTS", "3"))
    ANALYSIS_REQUEST_INTERVAL_SECONDS = float(os.getenv("ANALYSIS_REQUEST_INTERVAL_SECONDS", "2"))
    ANALYSIS_RETRY_ATTEMPTS = int(os.getenv("ANALYSIS_RETRY_ATTEMPTS", "3"))
    ANALYSIS_RETRY_BASE_SECONDS = float(os.getenv("ANALYSIS_RETRY_BASE_SECONDS", "2"))
    ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS", "60"))
    ANALYSIS_PROVIDER_MAX_COOLDOWN_SECONDS = float(os.getenv("ANALYSIS_PROVIDER_MAX_COOLDOWN_SECONDS", "300"))
    ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS", "86400"))
    ANALYSIS_LEASE_SECONDS = int(os.getenv("ANALYSIS_LEASE_SECONDS", "300"))
    ANALYSIS_VERSION = os.getenv("ANALYSIS_VERSION", "7")
    ANALYSIS_PROMPT_VERSION = os.getenv("ANALYSIS_PROMPT_VERSION", "6")
