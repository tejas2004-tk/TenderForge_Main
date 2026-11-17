import httpx
import logging
import math
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google import genai
from google.genai import errors as genai_errors

_logger = logging.getLogger(__name__)
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"


def _safe_provider_details(error, api_key):
    details = getattr(error, "details", None) or getattr(error, "body", None)
    payload = details.get("error", details) if isinstance(details, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("status") or payload.get("code") or getattr(error, "status", None)
    message = payload.get("message") or getattr(error, "message", None) or "No provider message returned."
    message = str(message).replace("\r", " ").replace("\n", " ")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", message)
    message = re.sub(r"(?i)(?:mongodb(?:\+srv)?://|(?:api[_-]?key|token|password)[=: ]+)[^\s]+", "[REDACTED]", message)
    message = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [REDACTED]", message)
    message = re.sub(r"([?&]key=)[^&\s]+", r"\1[REDACTED]", message, flags=re.I)
    return str(code or "unavailable")[:100], message[:500]


def _provider_retry_after(error):
    """Return a supported provider retry delay in seconds, if supplied."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = next((value for key, value in headers.items()
                        if str(key).casefold() == "retry-after"), None)
    if retry_after is not None:
        try:
            seconds = float(retry_after)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    details = getattr(error, "details", None) or getattr(error, "body", None)
    payload = details.get("error", details) if isinstance(details, dict) else {}
    candidates = [payload.get("retryDelay")] if isinstance(payload, dict) else []
    if isinstance(payload, dict):
        candidates.extend(payload.get(key) for key in ("retry_after", "retry-after", "retryAfter"))
        candidates.extend(
            item.get("retryDelay") for item in payload.get("details", [])
            if isinstance(item, dict)
        )
    for value in candidates:
        if value is None:
            continue
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*", str(value), re.I)
        if match:
            seconds = float(match.group(1)) / (1000 if (match.group(2) or "s").casefold() == "ms" else 1)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
    return None


def _is_daily_quota_error(error, provider_message):
    body = getattr(error, "details", None) or getattr(error, "body", None)
    rendered = f"{provider_message} {body}".casefold()
    return any(marker in rendered for marker in (
        "per day", "per-day", "daily quota", "requests per day", "daily limit",
    ))


class GeminiServiceError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def test_gemini_connection(api_key, model, settings=None):
    if not api_key:
        raise GeminiServiceError("Gemini is not configured. Set GEMINI_API_KEY in backend/.env.", 503)
    if not model:
        raise GeminiServiceError("Gemini is not configured. Set GEMINI_MODEL in backend/.env.", 503)
    started = time.perf_counter()
    settings = settings or {}
    timeout_ms = max(1000, int(settings.get("GEMINI_CONNECTION_TIMEOUT_MS", 90_000)))
    attempts = max(1, min(5, int(settings.get("GEMINI_CONNECTION_RETRY_ATTEMPTS", 3))))
    base_delay = max(0.0, float(settings.get("GEMINI_CONNECTION_RETRY_BASE_SECONDS", 1)))
    max_delay = max(0.0, float(settings.get("GEMINI_CONNECTION_RETRY_MAX_SECONDS", 30)))
    client = None
    try:
        client = genai.Client(api_key=api_key, http_options={
            "timeout": timeout_ms,
            # Disable SDK-owned retries so retry count, jitter, and provider
            # Retry-After handling are bounded and observable here.
            "retry_options": {"attempts": 1},
        })
        for attempt in range(1, attempts + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents="Reply with the single word connected.",
                    config={"temperature": 0, "max_output_tokens": 8},
                )
                answer = getattr(response, "text", None)
                if not isinstance(answer, str) or not answer.strip():
                    raise GeminiServiceError(
                        "Gemini accepted the request but returned no text. Check model response settings and safety filters.",
                        502,
                    )
                _logger.info(
                    "Gemini connection succeeded model=%s endpoint=%s http_status=200 duration_ms=%.1f attempts=%s",
                    model, _ENDPOINT, (time.perf_counter() - started) * 1000, attempt,
                )
                return answer.strip()
            except genai_errors.APIError as error:
                status = getattr(error, "code", None) or 502
                try:
                    status = int(status)
                except (TypeError, ValueError):
                    status = 502
                provider_code, provider_message = _safe_provider_details(error, api_key)
                if status in (503, 504) and attempt < attempts:
                    provider_delay = _provider_retry_after(error)
                    backoff_cap = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay = provider_delay if provider_delay is not None else random.uniform(0, backoff_cap)
                    # Honor server timing, but do not sleep past the configured
                    # retry budget; return the original error when it exceeds it.
                    if delay <= max_delay:
                        _logger.warning(
                            "Gemini temporary failure model=%s http_status=%s provider_code=%s "
                            "attempt=%s/%s retry_after_seconds=%.2f",
                            model, status, provider_code, attempt, attempts, delay,
                        )
                        time.sleep(delay)
                        continue
                _logger.warning(
                    "Gemini connection failed model=%s endpoint=%s http_status=%s provider_code=%s "
                    "duration_ms=%.1f attempt=%s provider_message=%s",
                    model, _ENDPOINT, status, provider_code,
                    (time.perf_counter() - started) * 1000, attempt, provider_message,
                )
                if status in (401, 403):
                    raise GeminiServiceError("Gemini authentication or permission failed. Check the API key and API access.", status) from error
                if status == 404:
                    raise GeminiServiceError(f"Gemini model '{model}' was not found or is unavailable to this API key.", 404) from error
                if status == 429:
                    reason = "daily quota exhausted" if _is_daily_quota_error(error, provider_message) else "rate limit reached"
                    raise GeminiServiceError(f"Gemini {reason}. Try again after the provider cooldown.", 429) from error
                if status in (503, 504):
                    raise GeminiServiceError(f"Gemini is temporarily unavailable (HTTP {status}). {provider_message}", status) from error
                if status >= 500:
                    raise GeminiServiceError(f"Gemini returned a server error (HTTP {status}). {provider_message}", status) from error
                raise GeminiServiceError(f"Gemini rejected the connection check (HTTP {status}). {provider_message}", status) from error
    except GeminiServiceError:
        raise
    except httpx.TimeoutException as error:
        _logger.warning(
            "Gemini connection timed out model=%s endpoint=%s http_status=unavailable "
            "duration_ms=%.1f error_type=%s",
            model, _ENDPOINT, (time.perf_counter() - started) * 1000, type(error).__name__,
        )
        raise GeminiServiceError("Gemini connection timed out. Check network access and try again.", 504) from error
    except httpx.NetworkError as error:
        _logger.warning(
            "Gemini connection network failure model=%s endpoint=%s http_status=unavailable "
            "duration_ms=%.1f error_type=%s",
            model, _ENDPOINT, (time.perf_counter() - started) * 1000, type(error).__name__,
        )
        raise GeminiServiceError("Gemini could not be reached due to a network failure. Check DNS, proxy, TLS, or firewall access.", 502) from error
    except (TimeoutError, ConnectionError, OSError) as error:
        _logger.warning(
            "Gemini connection network failure model=%s endpoint=%s http_status=unavailable "
            "duration_ms=%.1f error_type=%s",
            model, _ENDPOINT, (time.perf_counter() - started) * 1000, type(error).__name__,
        )
        raise GeminiServiceError("Gemini timed out or could not be reached. Check the backend network and try again.", 502) from error
    except (TypeError, ValueError) as error:
        _logger.error(
            "Gemini SDK configuration failure model=%s endpoint=%s duration_ms=%.1f error_type=%s",
            model, _ENDPOINT, (time.perf_counter() - started) * 1000, type(error).__name__,
        )
        raise GeminiServiceError("Gemini SDK configuration is invalid. Check the installed google-genai version and client options.", 500) from error
    except Exception as error:
        _logger.error(
            "Gemini SDK request failure model=%s endpoint=%s duration_ms=%.1f error_type=%s",
            model, _ENDPOINT, (time.perf_counter() - started) * 1000, type(error).__name__,
        )
        raise GeminiServiceError("Gemini SDK request failed unexpectedly. Check the backend SDK installation and configuration.", 502) from error
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
