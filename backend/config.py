import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


class Config:
    MONGODB_URI = os.getenv("MONGODB_URI", "")
    MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "tenderforge")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
    PORT = int(os.getenv("PORT", "5000"))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
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
    ANALYSIS_REQUEST_INTERVAL_SECONDS = float(os.getenv("ANALYSIS_REQUEST_INTERVAL_SECONDS", "2"))
    ANALYSIS_RETRY_ATTEMPTS = int(os.getenv("ANALYSIS_RETRY_ATTEMPTS", "3"))
    ANALYSIS_RETRY_BASE_SECONDS = float(os.getenv("ANALYSIS_RETRY_BASE_SECONDS", "2"))
    ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS", "60"))
    ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS = float(os.getenv("ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS", "86400"))
    ANALYSIS_LEASE_SECONDS = int(os.getenv("ANALYSIS_LEASE_SECONDS", "300"))
    ANALYSIS_VERSION = os.getenv("ANALYSIS_VERSION", "5")
    ANALYSIS_PROMPT_VERSION = os.getenv("ANALYSIS_PROMPT_VERSION", "4")
