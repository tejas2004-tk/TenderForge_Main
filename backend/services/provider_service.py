"""Provider-specific request adapters for new, policy-pinned analysis runs."""
from dataclasses import dataclass
import re
import time

import httpx
from google import genai


@dataclass(frozen=True)
class NormalizedProviderResponse:
    provider: str
    model: str
    content: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: float
    http_status: int | None = None
    request_id: str | None = None
    reasoning_present: bool = False


class ProviderRequestError(Exception):
    def __init__(self, provider, category, message, status_code=None,
                 retry_after_seconds=None, request_may_have_completed=False,
                 provider_code=None, finish_reason=None, input_tokens=None,
                 output_tokens=None, total_tokens=None, request_id=None,
                 reasoning_present=False):
        super().__init__(message)
        self.provider = provider
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.request_may_have_completed = request_may_have_completed
        self.provider_code = provider_code
        self.finish_reason = finish_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.request_id = request_id
        self.reasoning_present = bool(reasoning_present)


def _safe_message(value, api_key=""):
    message = str(value or "Provider request failed").replace("\r", " ").replace("\n", " ")
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", message)
    message = re.sub(r"(?i)(api[_-]?key|token|password)[=: ]+\S+", r"\1=[REDACTED]", message)
    return message[:500]


def _retry_after(response):
    value = response.headers.get("Retry-After") if response is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request_openai_compatible(provider, messages, model, api_key, base_url,
                               timeout_seconds, connect_timeout_seconds,
                               max_output_tokens, extra_headers=None):
    if not api_key or not model:
        raise ProviderRequestError(provider, "configuration", f"{provider} is not configured")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)) as client:
            body = {"model": model, "messages": messages, "temperature": 0}
            if provider == "groq":
                body["max_completion_tokens"] = max_output_tokens
                if model == "openai/gpt-oss-120b":
                    body["include_reasoning"] = False
            else:
                body["max_tokens"] = max_output_tokens
            response = client.post(url, headers=headers, json=body)
    except (httpx.ConnectTimeout, httpx.ConnectError) as error:
        raise ProviderRequestError(provider, "transport_before_send", _safe_message(error),
                                   request_may_have_completed=False) from error
    except httpx.TimeoutException as error:
        raise ProviderRequestError(provider, "timeout_uncertain", _safe_message(error),
                                   request_may_have_completed=True) from error
    except httpx.TransportError as error:
        raise ProviderRequestError(provider, "transport_uncertain", _safe_message(error),
                                   request_may_have_completed=True) from error
    elapsed = (time.perf_counter() - started) * 1000
    if response.status_code < 200 or response.status_code >= 300:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error_data = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error_data, dict):
            error_data = {}
        status = response.status_code
        category = "authentication" if status in (401, 403) else "http_error"
        raise ProviderRequestError(
            provider, category, _safe_message(error_data.get("message") or f"HTTP {status}", api_key),
            status_code=status, retry_after_seconds=_retry_after(response),
            request_may_have_completed=status >= 500,
            provider_code=_safe_message(error_data.get("code"), api_key)[:100],
        )
    try:
        payload = response.json()
        choice = payload["choices"][0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise (KeyError("message"))
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise ProviderRequestError(provider, "invalid_envelope", "Provider returned an invalid response envelope",
                                   status_code=response.status_code) from error
    content = message.get("content")
    finish_reason = choice.get("finish_reason")
    usage = payload.get("usage") or {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    request_id = response.headers.get("x-request-id") or payload.get("id")
    reasoning_present = bool(message.get("reasoning"))
    truncated = str(finish_reason or "").casefold() in {"length", "max_tokens", "max_completion_tokens"}
    if not isinstance(content, str) or not content.strip():
        if not truncated:
            category = "reasoning_only_response" if reasoning_present else "empty_content"
            raise ProviderRequestError(
                provider, category, "Provider returned no usable message content",
                status_code=response.status_code, finish_reason=finish_reason,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens, request_id=request_id,
                reasoning_present=reasoning_present,
            )
        # Keep the truncation signal and usage, but never substitute reasoning
        # for content. The analysis pipeline will split this batch safely.
        content = ""
    return NormalizedProviderResponse(
        provider, model, content, finish_reason, input_tokens, output_tokens, total_tokens,
        elapsed, response.status_code, request_id, reasoning_present,
    )


def request_groq(messages, *, model, api_key, timeout_seconds, connect_timeout_seconds, max_output_tokens):
    return _request_openai_compatible("groq", messages, model, api_key,
        "https://api.groq.com/openai/v1", timeout_seconds, connect_timeout_seconds, max_output_tokens)


def request_openrouter(messages, *, model, api_key, base_url, timeout_seconds,
                        connect_timeout_seconds, max_output_tokens, site_url=None, app_name="TenderForge"):
    extra = {"X-Title": app_name}
    if site_url:
        extra["HTTP-Referer"] = site_url
    return _request_openai_compatible("openrouter", messages, model, api_key, base_url,
        timeout_seconds, connect_timeout_seconds, max_output_tokens, extra)


def request_gemini(messages, *, model, api_key, timeout_seconds, max_output_tokens, response_schema):
    if not api_key or not model:
        raise ProviderRequestError("gemini", "configuration", "Gemini is not configured")
    system = next((item.get("content", "") for item in messages if item.get("role") == "system"), "")
    content = "\n\n".join(item.get("content", "") for item in messages if item.get("role") != "system")
    started = time.perf_counter()
    try:
        client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout_seconds * 1000), "retry_options": {"attempts": 1}})
        try:
            result = client.models.generate_content(model=model, contents=content, config={
                "system_instruction": system, "temperature": 0, "max_output_tokens": max_output_tokens,
                "response_mime_type": "application/json", "response_json_schema": response_schema,
            })
        finally:
            client.close()
    except Exception as error:
        status = getattr(error, "code", None) or getattr(error, "status_code", None)
        status = int(status) if str(status).isdigit() else None
        transport_error = type(error).__module__.startswith(("httpx", "httpcore"))
        uncertain = isinstance(error, (TimeoutError, ConnectionError, OSError)) or transport_error
        category = (
            "authentication" if status in (401, 403) else
            "http_error" if status else
            "timeout_uncertain" if uncertain else
            "provider_client_error"
        )
        retry = None
        response = getattr(error, "response", None)
        if response is not None:
            retry = _retry_after(response)
        raise ProviderRequestError("gemini", category, _safe_message(error, api_key), status,
                                   retry, request_may_have_completed=uncertain or bool(status and status >= 500),
                                   provider_code=getattr(error, "status", None)) from error
    usage = getattr(result, "usage_metadata", None)
    text = getattr(result, "text", None)
    return NormalizedProviderResponse(
        "gemini", model, text, str(getattr(result, "candidates", [{}])[0].finish_reason)
        if getattr(result, "candidates", None) else None,
        getattr(usage, "prompt_token_count", None), getattr(usage, "candidates_token_count", None),
        getattr(usage, "total_token_count", None), (time.perf_counter() - started) * 1000, 200,
    )
