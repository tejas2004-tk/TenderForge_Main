"""Isolated Phase 6 tests. All Gemini and collection interactions are mocked."""
import sys
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from flask import Flask
from google.genai import errors as genai_errors
from pymongo.errors import DuplicateKeyError, OperationFailure

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import tender_analysis_service as service


class MemoryCollection:
    """Small in-memory collection fake; it never connects to MongoDB."""
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.updates = []
        self.lock = threading.RLock()
        self.enforce_batch_unique = False

    class Cursor(list):
        def sort(self, key, direction=1):
            if isinstance(key, list):
                result = self.__class__(self)
                for field, order in reversed(key):
                    result = self.__class__(sorted(result, key=lambda row: row.get(field), reverse=order < 0))
                return result
            return self.__class__(sorted(self, key=lambda row: row.get(key), reverse=direction < 0))

    def _matches(self, row, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(row, condition) for condition in value):
                    return False
            elif key == "$and":
                if not all(self._matches(row, condition) for condition in value):
                    return False
            elif isinstance(value, dict):
                if "$in" in value and row.get(key) not in value["$in"]:
                    return False
                if "$ne" in value and row.get(key) == value["$ne"]:
                    return False
                if "$exists" in value and (key in row) != bool(value["$exists"]):
                    return False
                if "$lt" in value:
                    val = row.get(key)
                    if val is None or val >= value["$lt"]:
                        return False
                if "$lte" in value:
                    val = row.get(key)
                    if val is None or val > value["$lte"]:
                        return False
                if "$gt" in value:
                    val = row.get(key)
                    if val is None or val <= value["$gt"]:
                        return False
                if "$gte" in value:
                    val = row.get(key)
                    if val is None or val < value["$gte"]:
                        return False
            elif row.get(key) != value:
                return False
        return True

    def find(self, query=None, projection=None):
        return self.Cursor(dict(row) for row in self.rows if self._matches(row, query or {}))

    def find_one(self, query=None, sort=None):
        rows = list(self.find(query))
        if sort:
            for field, order in reversed(sort):
                rows.sort(key=lambda r: (r.get(field) is None, r.get(field)), reverse=order < 0)
        return rows[0] if rows else None

    def find_one_and_update(self, query, update, return_document=None, sort=None, **kwargs):
        with self.lock:
            candidates = [row for row in self.rows if self._matches(row, query)]
            if sort:
                for field, order in reversed(sort):
                    candidates.sort(key=lambda r: (r.get(field) is None, r.get(field)), reverse=order < 0)
            row = candidates[0] if candidates else None
            if row is not None:
                self._apply(row, update)
                return row
            return None

    def count_documents(self, query):
        return sum(self._matches(row, query) for row in self.rows)

    def update_one(self, query, update, upsert=False):
        with self.lock:
            self.updates.append((query, update))
            row = next((row for row in self.rows if self._matches(row, query)), None)
            if row is None and upsert:
                row = dict(query)
                row.update(update.get("$setOnInsert", {}))
                if self.enforce_batch_unique and any(
                    saved.get("analysis_id") == row.get("analysis_id")
                    and saved.get("batch_index") == row.get("batch_index")
                    for saved in self.rows
                ):
                    raise DuplicateKeyError("duplicate analysis_id + batch_index")
                self.rows.append(row)
            if row is not None:
                self._apply(row, update)
            return SimpleNamespace(matched_count=int(row is not None))

    def update_many(self, query, update):
        for row in self.rows:
            if self._matches(row, query):
                self._apply(row, update)

    def delete_many(self, query):
        self.rows = [row for row in self.rows if not self._matches(row, query)]

    def insert_one(self, row):
        saved = dict(row)
        saved.setdefault("_id", f"memory-id-{len(self.rows)}")
        self.rows.append(saved)
        return SimpleNamespace(inserted_id=saved["_id"])

    def bulk_write(self, operations, ordered=False):
        for operation in operations:
            self.update_one(operation._filter, operation._doc, operation._upsert)

    @staticmethod
    def _apply(row, update):
        for key, value in update.get("$set", {}).items():
            row[key] = value
        for key, value in update.get("$inc", {}).items():
            row[key] = row.get(key, 0) + value
        for key, value in update.get("$max", {}).items():
            row[key] = max(row.get(key, value), value)
        for key, value in update.get("$addToSet", {}).items():
            values = value.get("$each", [value]) if isinstance(value, dict) else [value]
            row.setdefault(key, [])
            for item in values:
                if item not in row[key]:
                    row[key].append(item)
        for key, value in update.get("$push", {}).items():
            row.setdefault(key, []).append(value)
        for key in update.get("$unset", {}):
            row.pop(key, None)


class Phase6AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            GEMINI_API_KEY="mock-key", GEMINI_MODEL="mock-model",
            ANALYSIS_MAX_INPUT_TOKENS=6000, ANALYSIS_MAX_OUTPUT_TOKENS=1400,
            ANALYSIS_RETRY_ATTEMPTS=3, ANALYSIS_RETRY_BASE_SECONDS=2,
            ANALYSIS_REQUEST_INTERVAL_SECONDS=0, ANALYSIS_MAX_CONCURRENT_REQUESTS=1,
        )
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()

    def _rate_error(self, headers=None, body=None):
        request = httpx.Request("POST", "https://example.invalid")
        response = httpx.Response(429, headers=headers or {}, request=request)
        return genai_errors.APIError(429, body or {"error": {"message": "temporary rate limit", "status": "RESOURCE_EXHAUSTED"}}, response=response)

    def _status_error(self, status_code, code="invalid_request", message="unsupported parameter"):
        request = httpx.Request("POST", "https://example.invalid")
        response = httpx.Response(status_code, request=request)
        return genai_errors.APIError(
            status_code,
            {"error": {"code": code, "status": "INVALID_ARGUMENT", "message": message}},
            response=response,
        )

    def test_provider_rejections_are_specific_persisted_and_not_retried(self):
        for status_code in (400, 401, 403, 404, 413):
            with self.subTest(status_code=status_code):
                fake_client = Mock()
                fake_client.models.generate_content.side_effect = self._status_error(
                    status_code, message=f"provider detail {status_code}"
                )
                analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
                batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
                analyses = MemoryCollection([analysis])
                batches = MemoryCollection([batch])
                with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
                    with self.assertRaises(service.AnalysisError) as caught:
                        service._gemini_request([{"content": "hello"}], analysis, batch,
                                              analyses, batches, "token")
                self.assertIn(f"HTTP {status_code}", str(caught.exception))
                if status_code not in (401, 403, 404):
                    self.assertIn(f"provider detail {status_code}", str(caught.exception))
                fake_client.models.generate_content.assert_called_once()
                diagnostic = analyses.rows[0]["provider_diagnostic"]
                self.assertEqual(diagnostic["status_code"], status_code)
                self.assertEqual(diagnostic["provider_code"], "INVALID_ARGUMENT")
                attempt = batches.rows[0]["api_attempts"][0]
                self.assertEqual(attempt["request_diagnostic"]["max_output_tokens"], 1400)
                self.assertNotIn("max_tokens", attempt["request_diagnostic"])

    def test_provider_5xx_is_retried_with_bounded_backoff(self):
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = [
            self._status_error(503, code="overloaded", message="temporary provider failure"),
            self._status_error(503, code="overloaded", message="temporary provider failure"),
        ]
        self.app.config["ANALYSIS_RETRY_ATTEMPTS"] = 2
        analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
        batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
        analyses = MemoryCollection([analysis])
        batches = MemoryCollection([batch])
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep") as sleep:
            with self.assertRaises(service.AnalysisError) as caught:
                service._gemini_request([{"content": "hello"}], analysis, batch,
                                      analyses, batches, "token")
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertEqual(fake_client.models.generate_content.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_provider_503_retry_after_pauses_without_ignoring_cooldown(self):
        request = httpx.Request("POST", "https://example.invalid")
        response = httpx.Response(503, headers={"Retry-After": "19"}, request=request)
        error = genai_errors.APIError(
            503,
            {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}},
            response=response,
        )
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = error
        analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
        batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
        analyses, batches = MemoryCollection([analysis]), MemoryCollection([batch])
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep") as sleep:
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._gemini_request([{"content": "hello"}], analysis, batch, analyses, batches, "token")
        self.assertEqual(caught.exception.retry_after, 19.0)
        self.assertFalse(caught.exception.daily_quota)
        self.assertEqual(fake_client.models.generate_content.call_count, 1)
        sleep.assert_not_called()
        attempt = batches.rows[0]["api_attempts"][0]
        self.assertEqual(attempt["provider_http_status"], 503)
        self.assertEqual(attempt["provider_diagnostic"]["provider_code"], "UNAVAILABLE")
        self.assertEqual(attempt["retry_after_seconds"], 19.0)

    def test_output_allowance_increases_and_stays_inside_tpm_budget(self):
        with patch.dict(service.current_app.config, {
            "ANALYSIS_MAX_OUTPUT_TOKENS": 4000, "ANALYSIS_MAX_TPM_TOKENS": 8000,
        }):
            requested = service._request_output_budget({"estimated_output_tokens": 100})
            self.assertEqual(requested, 1400)
            split_requested = service._request_output_budget(
                {"estimated_output_tokens": 100, "subdivision_depth": 1}
            )
            self.assertLess(split_requested, requested)
            self.assertEqual(service._tpm_output_budget(5893, requested), 1400)

    def test_timeout_is_retried_with_bounded_backoff(self):
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = TimeoutError("request timed out")
        self.app.config["ANALYSIS_RETRY_ATTEMPTS"] = 2
        analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
        batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep") as sleep:
            with self.assertRaises(service.AnalysisError) as caught:
                service._gemini_request([{"content": "hello"}], analysis, batch,
                                        MemoryCollection([analysis]), MemoryCollection([batch]), "token")
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(fake_client.models.generate_content.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(fake_client.models.generate_content.call_args.kwargs["config"]["response_mime_type"], "application/json")

    def test_structured_json_request_uses_google_json_schema(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=8),
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value = result
        analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
        batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            service._gemini_request([{"role": "system", "content": "system"}, {"role": "user", "content": "Return JSON"}],
                                    analysis, batch, MemoryCollection([analysis]), MemoryCollection([batch]), "token")
        call = fake_client.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "mock-model")
        self.assertEqual(call["contents"], "Return JSON")
        self.assertEqual(call["config"]["response_mime_type"], "application/json")
        self.assertIn("response_json_schema", call["config"])
        self.assertEqual(call["config"]["thinking_config"]["thinking_level"], "LOW")
        self.assertEqual(fake_client.models.generate_content.call_count, 1)

    def test_missing_and_incomplete_structured_json_are_classified(self):
        for content, expected in (
            ("", "incomplete_json"),
            ('{"overview": {}, "requirements": [', "incomplete_json"),
            ('{"overview": nope, "requirements": []}', "malformed_json"),
            ('{"overview": {}}', "incomplete_json"),
        ):
            with self.subTest(content=content):
                with self.assertRaises(service.AnalysisResponseError) as caught:
                    service._parse_response(content)
                self.assertEqual(caught.exception.error_category, expected)

    def test_thinking_usage_is_recorded_and_included_in_provider_totals(self):
        result = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            text='{"overview": {}, "requirements": []}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=44, candidates_token_count=12,
                thoughts_token_count=900, tool_use_prompt_token_count=3,
                total_token_count=959,
            ),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            service._process_batch(analysis, "lease", analyses, batches, MemoryCollection(), pages, chunks)
        self.assertEqual(analyses.rows[0]["actual_thinking_tokens"], 900)
        self.assertEqual(analyses.rows[0]["actual_provider_total_tokens"], 959)
        self.assertEqual(analyses.rows[0]["total_tokens"], 959)
        attempt = batches.rows[0]["api_attempts"][0]
        self.assertEqual(attempt["provider_http_status"], 200)
        self.assertEqual(attempt["provider_thinking_tokens"], 900)
        self.assertEqual(attempt["provider_total_tokens"], 959)
        self.assertEqual(batches.rows[0]["actual_thinking_tokens"], 900)

    def test_empty_structured_generation_is_rejected_locally(self):
        empty_result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=""))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=0),
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value = empty_result
        analysis = {"_id": "analysis", "model_id": "mock-model", "processing_token": "token"}
        batch = {"_id": "batch", "batch_index": 0, "estimated_output_tokens": 900}
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            result = service._gemini_request(
                [{"content": "Return JSON"}], analysis, batch,
                MemoryCollection([analysis]), MemoryCollection([batch]), "token",
            )
        with self.assertRaises(service.AnalysisError) as caught:
            service._parse_response(service._response_text(result[0]))
        self.assertIn("empty analysis response", str(caught.exception))

    def test_summary_exposes_safe_provider_diagnostic(self):
        summary = service._summary({
            "_id": "analysis",
            "status": "failed",
            "provider_diagnostic": {
                "operation": "google.genai.models.generate_content",
                "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
                "status_code": 400,
                "provider_code": "invalid_request",
                "provider_type": "invalid_request_error",
                "provider_message": "bad parameter",
                "model": "openai/gpt-oss-120b",
                "estimated_input_tokens": 123,
                "max_output_tokens": 1200,
                "temperature": 0,
                "response_mime_type": "application/json",
            },
        })
        self.assertEqual(summary["provider_diagnostic"]["status_code"], 400)
        self.assertEqual(summary["provider_diagnostic"]["provider_message"], "bad parameter")
        self.assertNotIn("api_key", json.dumps(summary).casefold())

    def test_new_analysis_records_gemini_provider_and_fixed_model(self):
        with patch.dict(service.current_app.config, {"GEMINI_MODEL": "gemini-test-model", "ANALYSIS_VERSION": "6"}):
            record = service._new_analysis_record(
                {"analysis_version": "7", "user_id": "u", "tender_id": "t", "file_hash": "h"},
                {"page_count": 1, "extracted_pages": 1},
            )
        self.assertEqual(record["provider_policy_version"], 1)
        self.assertEqual(record["provider_order"], ["groq", "openrouter", "gemini"])
        self.assertEqual(record["provider_models"]["gemini"], "gemini-test-model")
        self.assertEqual(record["provider_models"]["groq"], "")

    def test_new_policy_run_fails_over_and_persists_provider_attempts(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "explicit-model", "openrouter": "openai/gpt-oss-120b"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        analyses, batches = MemoryCollection([analysis]), MemoryCollection([batch])
        good = SimpleNamespace(
            provider="openrouter", model="openai/gpt-oss-120b",
            content='{"overview":{},"requirements":[]}', finish_reason="stop",
            input_tokens=10, output_tokens=20, total_tokens=30, latency_ms=12.0,
            http_status=200, request_id="request-1",
        )
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "groq-test", "OPENROUTER_API_KEY": "router-test",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", side_effect=service.ProviderRequestError(
            "groq", "http_error", "busy", status_code=503)), patch.object(
            service, "request_openrouter", return_value=good,
        ) as router:
            result = service._provider_request(
                [{"role": "user", "content": "source"}], analysis, batch, analyses, batches, "lease",
            )
        self.assertEqual(service._response_text(result[0]), '{"overview":{},"requirements":[]}')
        self.assertEqual(batches.rows[0]["provider"], "openrouter")
        self.assertEqual(batches.rows[0]["model_id"], "openai/gpt-oss-120b")
        attempts = batches.rows[0]["api_attempts"]
        self.assertEqual([item["provider"] for item in attempts], ["groq", "openrouter"])
        self.assertEqual(analyses.rows[0]["selected_provider"], "openrouter")
        self.assertNotIn("router-test", repr(attempts))
        router.assert_called_once()

    def test_groq_http_200_reasoning_only_falls_back_without_persisting_reasoning(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "openai/gpt-oss-120b", "openrouter": "openai/gpt-oss-120b"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        analyses, batches = MemoryCollection([analysis]), MemoryCollection([batch])
        good = SimpleNamespace(
            provider="openrouter", model="openai/gpt-oss-120b",
            content='{"overview":{},"requirements":[]}', finish_reason="stop",
            input_tokens=10, output_tokens=20, total_tokens=30, latency_ms=12.0,
            http_status=200, request_id="router-1", reasoning_present=False,
        )
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "groq-test", "OPENROUTER_API_KEY": "router-test",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", side_effect=service.ProviderRequestError(
            "groq", "reasoning_only_response", "Provider returned no usable message content",
            status_code=200, finish_reason="stop", input_tokens=5, output_tokens=9,
            total_tokens=14, request_id="groq-1", reasoning_present=True,
        )), patch.object(service, "request_openrouter", return_value=good) as router:
            result = service._provider_request(
                [{"role": "user", "content": "prompt"}], analysis, batch, analyses, batches, "lease",
            )
        attempts = batches.rows[0]["api_attempts"]
        self.assertEqual([item["provider"] for item in attempts], ["groq", "openrouter"])
        self.assertEqual(attempts[0]["provider_http_status"], 200)
        self.assertEqual(attempts[0]["finish_reason"], "stop")
        self.assertEqual(attempts[0]["provider_total_tokens"], 14)
        self.assertTrue(attempts[0]["reasoning_present"])
        self.assertNotIn("reasoning text", repr(attempts))
        self.assertEqual(service._response_text(result[0]), '{"overview":{},"requirements":[]}')
        router.assert_called_once()

    def test_groq_valid_http_200_content_is_returned_without_fallback(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "openai/gpt-oss-120b", "openrouter": "m"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        good = SimpleNamespace(
            provider="groq", model="openai/gpt-oss-120b", content='{"overview":{},"requirements":[]}',
            finish_reason="stop", input_tokens=10, output_tokens=20, total_tokens=30,
            latency_ms=8.0, http_status=200, request_id="groq-ok", reasoning_present=True,
        )
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "groq-test", "OPENROUTER_API_KEY": "router-test",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", return_value=good) as groq, \
             patch.object(service, "request_openrouter") as fallback:
            result = service._provider_request(
                [{"role": "user", "content": "prompt"}], analysis, batch,
                MemoryCollection([analysis]), MemoryCollection([batch]), "lease",
            )
        self.assertEqual(service._response_text(result[0]), '{"overview":{},"requirements":[]}')
        self.assertEqual(groq.call_args.kwargs["max_output_tokens"], 400)
        fallback.assert_not_called()

    def test_new_policy_429_moves_immediately_to_next_provider_once(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "openai/gpt-oss-120b", "openrouter": "m"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        good = SimpleNamespace(
            provider="openrouter", model="m", content='{"overview":{},"requirements":[]}',
            finish_reason="stop", input_tokens=1, output_tokens=1, total_tokens=2,
            latency_ms=1.0, http_status=200, request_id="r2", reasoning_present=False,
        )
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", side_effect=service.ProviderRequestError(
            "groq", "http_error", "limited", status_code=429, retry_after_seconds=120,
        )) as groq, patch.object(service, "request_openrouter", return_value=good) as router:
            service._provider_request([{"role": "user", "content": "prompt"}], analysis, batch,
                                     MemoryCollection([analysis]), MemoryCollection([batch]), "lease")
        groq.assert_called_once()
        router.assert_called_once()

    def test_invalid_json_falls_back_for_only_the_affected_chunk(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "g-model", "openrouter": "o-model"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": service.PROMPT_VERSION,
                 "estimated_output_tokens": 500}
        invalid = SimpleNamespace(provider="groq", model="g-model", content="not json",
                                  finish_reason="stop", input_tokens=10, output_tokens=20,
                                  total_tokens=30, latency_ms=1, http_status=200, request_id="bad")
        valid = SimpleNamespace(provider="openrouter", model="o-model",
                                content='{"overview":{},"requirements":[]}', finish_reason="stop",
                                input_tokens=10, output_tokens=20, total_tokens=30,
                                latency_ms=1, http_status=200, request_id="good")
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", return_value=invalid) as groq, \
             patch.object(service, "request_openrouter", return_value=valid) as router:
            result = service._provider_request([{"role": "user", "content": "prompt"}], analysis, batch,
                                               MemoryCollection([analysis]), MemoryCollection([batch]), "lease")
        self.assertIn('"requirements":[]', service._response_text(result[0]))
        groq.assert_called_once()
        router.assert_called_once()

    def test_truncated_empty_groq_content_reaches_truncation_handler_contract(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "openai/gpt-oss-120b", "openrouter": "m"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        truncated = SimpleNamespace(
            provider="groq", model="openai/gpt-oss-120b", content="", finish_reason="length",
            input_tokens=20, output_tokens=100, total_tokens=120, latency_ms=5.0,
            http_status=200, request_id="truncated", reasoning_present=True,
        )
        with patch.dict(service.current_app.config, {
            "GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 400, "ANALYSIS_MAX_TPM_TOKENS": 800,
        }), patch.object(service, "request_groq", return_value=truncated), \
             patch.object(service, "request_openrouter") as fallback:
            result = service._provider_request([{"role": "user", "content": "prompt"}], analysis, batch,
                                               MemoryCollection([analysis]), MemoryCollection([batch]), "lease")
        self.assertEqual(service._response_text(result[0]), "")
        self.assertEqual(service._response_finish_reason(result[0]), "MAX_TOKENS")
        self.assertEqual(result[1:6], (service._estimate_tokens("prompt"), 20, 100, None, 120))
        fallback.assert_not_called()

    def test_empty_groq_length_response_splits_batch_without_findings(self):
        result = SimpleNamespace(
            provider="groq", model="openai/gpt-oss-120b", content="", finish_reason="length",
            input_tokens=20, output_tokens=100, total_tokens=120, latency_ms=5.0,
            http_status=200, request_id="truncated", reasoning_present=True,
        )
        analysis, batch, analyses, batches, pages, chunks, _ = self._batch_inputs(None)
        analysis.update({"provider_policy_version": 1, "provider_order": ["groq", "openrouter"],
                         "provider_models": {"groq": "openai/gpt-oss-120b", "openrouter": "m"}})
        batch["chunk_refs"] = [{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}]
        batches.rows[0]["chunk_refs"] = list(batch["chunk_refs"])
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Page two source.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        pages.rows.append({"page_number": 2, "part_index": 0, "complete": True, "text": "Page two source."})
        requirements = MemoryCollection()
        with patch.dict(service.current_app.config, {"GROQ_API_KEY": "test", "ANALYSIS_MAX_OUTPUT_TOKENS": 1400}), \
             patch.object(service, "request_groq", return_value=result), \
             patch.object(service, "request_openrouter") as fallback:
            self.assertTrue(service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks))
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual(batches.rows[0]["finish_reason"], "MAX_TOKENS")
        self.assertEqual(requirements.rows, [])
        children = [row for row in batches.rows if row.get("parent_batch_id") == "batch"]
        self.assertEqual(len(children), 2)
        fallback.assert_not_called()

    def test_new_policy_auth_failure_stops_without_fallback(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "explicit-model", "openrouter": "m"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        with patch.dict(service.current_app.config, {"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o"}), \
             patch.object(service, "request_groq", side_effect=service.ProviderRequestError(
                 "groq", "authentication", "unauthorized", status_code=401)), \
             patch.object(service, "request_openrouter") as fallback:
            with self.assertRaises(service.AnalysisError):
                service._provider_request([{"role": "user", "content": "prompt"}], analysis, batch,
                                          MemoryCollection([analysis]), MemoryCollection([batch]), "lease")
        fallback.assert_not_called()

    def test_uncertain_timeout_pauses_and_pins_same_provider_for_resume(self):
        analysis = {"_id": "a", "processing_token": "lease", "provider_order": ["groq", "openrouter"],
                    "provider_models": {"groq": "explicit-model", "openrouter": "m"}}
        batch = {"_id": "b", "attempt_count": 0, "prompt_version": "5", "estimated_output_tokens": 300}
        batches = MemoryCollection([batch])
        with patch.dict(service.current_app.config, {"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o"}), \
             patch.object(service, "request_groq", side_effect=service.ProviderRequestError(
                 "groq", "timeout_uncertain", "timed out", request_may_have_completed=True)), \
             patch.object(service, "request_openrouter") as fallback:
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._provider_request([{"role": "user", "content": "prompt"}], analysis, batch,
                                          MemoryCollection([analysis]), batches, "lease")
        self.assertEqual(caught.exception.reason, "uncertain_timeout")
        self.assertEqual(batches.rows[0]["preferred_provider"], "groq")
        self.assertTrue(batches.rows[0]["uncertain_timeout_pending"])
        fallback.assert_not_called()

    def test_legacy_groq_analysis_remains_readable_and_cannot_resume(self):
        user = {"_id": "user"}
        tender = {"_id": "tender", "sha256": "hash"}
        legacy = {"_id": "legacy", "model_id": "llama-3.1-8b-instant", "status": "failed"}
        summary = service._summary(legacy)
        self.assertEqual(summary["provider"], "groq")
        collections = (MemoryCollection([legacy]), MemoryCollection(), MemoryCollection(),
                       MemoryCollection(), MemoryCollection(), MemoryCollection())
        with patch.object(service, "_find_owned_tender", return_value=tender), \
                patch.object(service, "_ready_extraction"), \
                patch.object(service, "_collections", return_value=collections), \
                patch.object(service, "_latest_analysis", return_value=legacy), \
                patch.object(service, "_advance") as advance:
            with self.assertRaises(service.AnalysisError) as caught:
                service.resume_analysis(user, "tender")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("fresh Gemini analysis", str(caught.exception))
        advance.assert_not_called()

    def test_retry_after_numeric_and_http_date_are_respected(self):
        error = self._rate_error({"Retry-After": "12.5"})
        self.assertEqual(service._retry_after(error), 12.5)
        now = datetime(2026, 11, 17, 6, 28, 5, tzinfo=timezone.utc)
        with patch.object(service, "_now", return_value=now):
            error = self._rate_error({"Retry-After": format_datetime(now + timedelta(seconds=15), usegmt=True)})
            self.assertEqual(service._retry_after(error), 15.0)

    def test_missing_reset_headers_use_persistable_fallback_without_sleeping(self):
        analysis = {"_id": "analysis", "model_id": "mock-model"}
        batch = {"_id": "batch", "batch_index": 0}
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = self._rate_error({"Retry-After": "not-a-delay"})
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep") as sleep:
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._gemini_request([{"content": "hello"}], analysis, batch,
                                      MemoryCollection(), MemoryCollection(), "token")
        self.assertEqual(caught.exception.retry_after, 60)
        fake_client.models.generate_content.assert_called_once()
        sleep.assert_not_called()

    def test_provider_reset_headers_are_used_when_retry_after_is_missing(self):
        error = self._rate_error({"x-ratelimit-reset-tokens": "2m3s", "x-ratelimit-reset-requests": "10s"})
        self.assertEqual(service._rate_limit_cooldown(error), 123)

    def test_retry_delay_in_error_body_is_used(self):
        error = self._rate_error(
            body={"error": {"message": "Please try again in 3.3 seconds.", "code": "rate_limit_exceeded"}}
        )
        self.assertEqual(service._rate_limit_cooldown(error), 3.3)
        error = self._rate_error(
            body={"error": {"retry_after": "3.3s", "code": "rate_limit_exceeded"}}
        )
        self.assertEqual(service._rate_limit_cooldown(error), 3.3)

    def test_conflicting_retry_metadata_prefers_retry_after_and_logs_selection(self):
        error = self._rate_error({
            "Retry-After": "3.3",
            "x-ratelimit-reset-tokens": "3600000",
        })
        with patch.object(service._logger, "warning") as warning:
            cooldown, source = service._rate_limit_cooldown_details(error)
        self.assertEqual(cooldown, 3.3)
        self.assertEqual(source, "Retry-After header")
        warning.assert_called_once()

    def test_numeric_reset_metadata_supports_milliseconds_without_hours_of_pause(self):
        now = datetime(2026, 11, 17, 6, 28, 5, tzinfo=timezone.utc)
        with patch.object(service, "_now", return_value=now):
            self.assertEqual(service._parse_reset_seconds("3600000"), 3600)
            self.assertAlmostEqual(
                service._parse_reset_seconds(str(int((now.timestamp() + 3.3) * 1000))),
                3.3,
                places=2,
            )

    def test_valid_retry_after_is_used_for_retry(self):
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = self._rate_error({"Retry-After": "17"})
        sleeps = []
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep", side_effect=lambda seconds: sleeps.append(seconds)):
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._gemini_request([{"content": "hello"}], {"_id": "a", "model_id": "m"},
                                      {"_id": "b", "batch_index": 0}, MemoryCollection(), MemoryCollection(), "t")
        self.assertEqual(caught.exception.retry_after, 17.0)
        self.assertEqual(sleeps, [])

    def test_output_budget_is_capped_by_combined_tpm_limit(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=23, completion_tokens=12),
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value = result
        with patch.dict(service.current_app.config, {"ANALYSIS_MAX_TPM_TOKENS": 300}), \
                patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            service._gemini_request(
                [{"content": "hello"}],
                {"_id": "a", "model_id": "m"},
                {"_id": "b", "batch_index": 0, "estimated_output_tokens": 900},
                MemoryCollection(), MemoryCollection(), "t",
            )
        self.assertEqual(fake_client.models.generate_content.call_args.kwargs["config"]["max_output_tokens"], 277)

    def test_temporary_rate_limit_pauses_with_resume_delay(self):
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = self._rate_error({"Retry-After": "9"})
        self.app.config["ANALYSIS_RETRY_ATTEMPTS"] = 1
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._gemini_request([{"content": "x"}], {"_id": "a", "model_id": "m"},
                                      {"_id": "b", "batch_index": 0}, MemoryCollection(), MemoryCollection(), "t")
        self.assertFalse(caught.exception.daily_quota)
        self.assertEqual(caught.exception.retry_after, 9.0)

    def test_invalid_retry_metadata_uses_short_configured_fallback(self):
        error = self._rate_error({
            "Retry-After": "not-a-delay",
            "x-ratelimit-reset-tokens": "not-a-reset",
        })
        with patch.dict(service.current_app.config, {"ANALYSIS_RATE_LIMIT_FALLBACK_SECONDS": 60}):
            self.assertEqual(service._rate_limit_cooldown(error), 60)

    def test_pause_persists_provider_delay_and_retry_timestamp(self):
        fixed_now = datetime(2026, 11, 17, 6, 28, 5, tzinfo=timezone.utc)
        analysis = {"_id": "analysis", "status": "pending", "plan_ready": True, "total_batches": 1,
                    "active_started_at": fixed_now, "coverage": {}}
        analyses = MemoryCollection([analysis])
        batches = MemoryCollection([{"_id": "batch", "analysis_id": "analysis", "status": "processing"}])
        with patch.object(service, "_now", return_value=fixed_now), \
                patch.object(service, "_process_batch",
                             side_effect=service.AnalysisPaused("temporary", retry_after=3.3)):
            result = service._advance(analysis, analyses, batches, MemoryCollection(), MemoryCollection(), MemoryCollection())
        self.assertEqual(result["retry_after_seconds"], 3.3)
        self.assertEqual(analyses.rows[0]["pause_until"], fixed_now + timedelta(seconds=3.3))

    def test_daily_quota_pauses_separately_without_retry(self):
        fake_client = Mock()
        fake_client.models.generate_content.side_effect = self._rate_error(
            {"Retry-After": "90"}, {"error": {"message": "Daily quota exceeded", "code": "insufficient_quota"}}
        )
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            with self.assertRaises(service.AnalysisPaused) as caught:
                service._gemini_request([{"content": "x"}], {"_id": "a", "model_id": "m"},
                                      {"_id": "b", "batch_index": 0}, MemoryCollection(), MemoryCollection(), "t")
        self.assertTrue(caught.exception.daily_quota)
        self.assertEqual(caught.exception.retry_after, 90)
        fake_client.models.generate_content.assert_called_once()

    def _batch_inputs(self, result):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash",
                    "provider": "gemini", "model_id": "mock-model", "overview_fields": {}}
        batch = {"_id": "batch", "analysis_id": "analysis", "batch_index": 0,
                 "user_id": "user", "tender_id": "tender", "file_hash": "hash",
                 "is_overview_batch": True, "prompt_version": "5",
                 "chunk_refs": [{"chunk_index": 1, "page_number": 1}], "status": "pending"}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([dict(analysis, processing_token="lease")])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": "The bidder shall submit certificate.",
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        pages = MemoryCollection([{"page_number": 1, "part_index": 0, "complete": True,
                                   "text": "The bidder shall submit certificate."}])
        fake_client = Mock()
        fake_client.models.generate_content.return_value = result
        return analysis, batch, analyses, batches, pages, chunks, fake_client

    def test_incomplete_json_keeps_usage_and_splits_for_recovery(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"requirements": ['))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        requirements = MemoryCollection()
        with patch.dict(service.current_app.config, {"ANALYSIS_MIN_SEGMENT_CHARS": 1}), \
                patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            self.assertTrue(service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks))
        saved = analyses.rows[0]
        self.assertEqual((saved["request_count"], saved["actual_input_tokens"], saved["actual_output_tokens"]), (1, 44, 12))
        self.assertEqual(requirements.rows, [])
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual(len([row for row in batches.rows if row.get("parent_batch_id") == "batch"]), 2)
        self.assertEqual(batches.rows[0]["failure_category"], "incomplete_json")

    def test_advance_marks_malformed_batch_failed_and_resume_eligible(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"requirements": ['))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        analysis.update({"plan_ready": True, "status": "pending", "total_batches": 1})
        analyses = MemoryCollection([dict(analysis)])
        requirements = MemoryCollection()
        recovered = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=10),
        )
        client.models.generate_content.side_effect = [result, recovered, recovered]
        with patch.dict(service.current_app.config, {"ANALYSIS_MIN_SEGMENT_CHARS": 1}), \
                patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            response = service._advance(analyses.rows[0], analyses, batches, requirements, pages, chunks)
            self.assertEqual(response["status"], "needs_review")
            self.assertEqual(batches.rows[0]["status"], "split")
            self.assertIsNone(analyses.rows[0].get("processing_token"))
        self.assertEqual(response["status"], "needs_review")
        self.assertTrue(all(row["status"] == "completed" for row in batches.rows if row.get("parent_batch_id") == "batch"))

    def test_truncated_response_is_failed_before_findings_are_saved(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=1400),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        batch["chunk_refs"] = [{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}]
        batches.rows[0]["chunk_refs"] = list(batch["chunk_refs"])
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Page two requirement.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        pages.rows.append({"page_number": 2, "part_index": 0, "complete": True, "text": "Page two requirement."})
        requirements = MemoryCollection()
        untouched_pending = {"_id": "already-pending", "analysis_id": "analysis", "batch_index": 1,
                             "status": "pending", "chunk_refs": [{"chunk_index": 3, "page_number": 3}]}
        batches.rows.append(untouched_pending)
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"), \
                patch.object(service, "_save_requirements") as save:
            service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks)
        save.assert_not_called()
        self.assertEqual(batches.rows[0]["status"], "split")
        children = [row for row in batches.rows if row.get("parent_batch_id") == "batch"]
        self.assertEqual(len(children), 2)
        self.assertEqual({tuple((ref["chunk_index"], ref["page_number"]) for ref in child["chunk_refs"]) for child in children},
                         {((1, 1),), ((2, 2),)})
        self.assertTrue(all(child["user_id"] == "user" and child["tender_id"] == "tender" and child["file_hash"] == "hash" for child in children))
        self.assertTrue(all(child["is_overview_batch"] and child["prompt_version"] == service.PROMPT_VERSION for child in children))
        self.assertGreater(analysis["analysis_work_policy"]["adaptive_max_source_chars"], 0)
        self.assertEqual(untouched_pending["status"], "pending")
        self.assertEqual(analyses.rows[0]["actual_output_tokens"], 1400)
        self.assertEqual(requirements.rows, [])

    def test_child_tasks_resume_without_resending_split_parent(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
        )
        analysis, batch, analyses, _, pages, chunks, client = self._batch_inputs(response)
        parent = dict(batch, status="split", is_overview_batch=False,
                      chunk_refs=[{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}])
        child = {"_id": "child", "analysis_id": "analysis", "user_id": "user", "tender_id": "tender",
                 "file_hash": "hash", "batch_index": 2, "parent_batch_id": "batch", "status": "pending",
                 "attempt_count": 0, "is_overview_batch": False, "prompt_version": "5",
                 "chunk_refs": [{"chunk_index": 2, "page_number": 2}]}
        batches = MemoryCollection([parent, child])
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Page two requirement.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        requirements = MemoryCollection()
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks)
        sent = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("SOURCE PAGE 2", sent)
        self.assertNotIn("SOURCE PAGE 1", sent)
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual(batches.rows[1]["status"], "completed")
        self.assertEqual(batches.rows[1]["prompt_version"], service.PROMPT_VERSION)
        self.assertFalse(batches.rows[1]["is_overview_batch"])

    def test_resume_subdivides_manual_child_and_preserves_completed_findings(self):
        evidence = "The bidder must have supplied similar equipment for 3 years."
        result = SimpleNamespace(
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            text=json.dumps({"overview": {}, "requirements": [{
                "category": "Experience Requirements",
                "requirement_title": "Similar equipment experience",
                "description": "The bidder must have supplied similar equipment for 3 years",
                "requirement_value": "3 years", "mandatory": True,
                "requirement_status": "mandatory", "source_page": 1,
                "evidence_text": evidence, "extraction_status": "extracted",
            }]}),
            usage_metadata=SimpleNamespace(
                prompt_token_count=40, candidates_token_count=80,
                thoughts_token_count=40, total_token_count=160,
            ),
        )
        analysis = {
            "_id": "analysis", "status": "needs_review", "plan_ready": True,
            "total_batches": 2, "page_count": 1, "user_id": "user",
            "tender_id": "tender", "file_hash": "hash", "provider": "gemini",
            "model_id": "mock-model", "overview_fields": {},
            "processing_token": None, "created_at": service._now(),
        }
        analyses = MemoryCollection([analysis])
        completed = {
            "_id": "completed", "analysis_id": "analysis", "batch_index": 0,
            "user_id": "user", "tender_id": "tender", "file_hash": "hash",
            "status": "completed", "attempt_count": 1, "api_call_count": 1,
            "chunk_refs": [{"chunk_index": 1, "page_number": 1}],
        }
        manual = {
            "_id": "manual-child", "analysis_id": "analysis", "batch_index": 2,
            "parent_batch_id": "split-parent", "user_id": "user", "tender_id": "tender",
            "file_hash": "hash", "status": "manual_review", "attempt_count": 1,
            "api_call_count": 1, "finish_reason": "MAX_TOKENS",
            "manual_review_reason": "output_truncated_single_reference",
            "chunk_refs": [{"chunk_index": 2, "page_number": 1}],
            "analysis_work_policy": {"max_split_depth": 2, "min_segment_chars": 1},
            "prompt_version": "5", "is_overview_batch": False,
        }
        batches = MemoryCollection([completed, manual])
        pages = MemoryCollection([{
            "user_id": "user", "tender_id": "tender", "file_hash": "hash",
            "page_number": 1, "part_index": 0, "complete": True, "text": evidence,
        }])
        chunks = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash",
             "chunk_index": index, "page_number": 1, "text": evidence}
            for index in (1, 2)
        ])
        requirements = MemoryCollection()
        service._save_requirements([self._raw_requirement(1, evidence)], analysis, completed, pages, requirements)
        self.assertEqual(len(requirements.rows), 1)
        fake_client = Mock()
        fake_client.models.generate_content.return_value = result
        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            service._advance(analyses.rows[0], analyses, batches, requirements, pages, chunks)
        self.assertEqual(fake_client.models.generate_content.call_count, 2)
        completed_saved = next(row for row in batches.rows if row["_id"] == "completed")
        manual_saved = next(row for row in batches.rows if row["_id"] == "manual-child")
        self.assertEqual(completed_saved["status"], "completed")
        self.assertEqual(completed_saved["api_call_count"], 1)
        self.assertEqual(manual_saved["status"], "split")
        self.assertEqual(manual_saved["manual_review_retry_count"], 1)
        self.assertEqual(len([row for row in batches.rows if row.get("parent_batch_id") == "manual-child"]), 2)
        self.assertEqual(len(requirements.rows), 1)
        self.assertEqual(
            {row["evidence_text"] for row in requirements.rows[0]["source_evidence"]},
            {evidence},
        )

    def test_splitting_overview_batch_preserves_overview_extraction(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps({
                "overview": {"tender_title": {"value": "TenderForge Sample", "source_page": 1,
                                               "evidence_text": "Tender title: TenderForge Sample."}},
                "requirements": [],
            })))],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=20),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(response)
        batch["chunk_refs"] = [{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}]
        batches.rows[0]["chunk_refs"] = list(batch["chunk_refs"])
        chunks.rows[0]["text"] = "Tender title: TenderForge Sample. The bidder shall submit certificate."
        pages.rows[0]["text"] = chunks.rows[0]["text"]
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Second page text.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        pages.rows.append({"page_number": 2, "part_index": 0, "complete": True, "text": "Second page text."})
        service._split_truncated_batch(batches.rows[0], batches, analyses=analyses)
        children = [row for row in batches.rows if row.get("parent_batch_id") == "batch"]
        self.assertTrue(all(row["is_overview_batch"] and row["prompt_version"] == "5" for row in children))
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"), \
                patch.object(service, "_validated_overview_fields", wraps=service._validated_overview_fields) as validate:
            service._process_batch(analysis, "lease", analyses, batches, MemoryCollection(), pages, chunks)
        sent_prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("tender identity", sent_prompt)
        self.assertIn("tender_title", validate.call_args.args[0])

    def test_planned_first_batch_persists_overview_flag_and_prompt_version(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        chunks = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "chunk_index": 1,
             "page_number": 1, "text": "The bidder shall submit the required certificate."},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "chunk_index": 2,
             "page_number": 2, "text": "General tender notes."},
        ])
        batches = MemoryCollection()
        with patch.dict(service.current_app.config, {"ANALYSIS_PROMPT_VERSION": "4"}):
            total, _ = service._plan_batches(analysis, batches, chunks, page_count=2)
        self.assertEqual(total, 1)
        self.assertTrue(batches.rows[0]["is_overview_batch"])
        self.assertEqual(batches.rows[0]["prompt_version"], "4")

    def test_universal_planner_covers_long_paragraph_table_and_scanned_page_idempotently(self):
        paragraph = "The bidder shall submit the certificate with the tender. " * 900
        table = "| Item | Eligibility condition | Evidence |\n|---|---|---|\n" + (
            "| Bidder | shall provide a valid certificate | certificate copy |\n" * 700
        )
        analysis = {"_id": "synthetic", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        chunks = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "chunk_index": 1,
             "page_number": 1, "text": paragraph, "char_start": 0, "char_end": len(paragraph)},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "chunk_index": 2,
             "page_number": 2, "text": table, "char_start": 0, "char_end": len(table)},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "chunk_index": 3,
             "page_number": 3, "text": "", "char_start": 0, "char_end": 0},
        ])
        batches = MemoryCollection()
        with patch.dict(service.current_app.config, {
            "ANALYSIS_MAX_INPUT_TOKENS": 4500, "ANALYSIS_MAX_OUTPUT_TOKENS": 1000,
            "ANALYSIS_BATCH_TARGET_OUTPUT_TOKENS": 800, "ANALYSIS_MIN_SEGMENT_CHARS": 80,
        }):
            first_total, _ = service._plan_batches(analysis, batches, chunks, page_count=3)
            first_ids = {row["_id"] for row in batches.rows}
            second_total, _ = service._plan_batches(analysis, batches, chunks, page_count=3)
        self.assertGreater(first_total, 2)
        self.assertEqual(second_total, first_total)
        self.assertEqual({row["_id"] for row in batches.rows}, first_ids)
        for index, text in ((1, paragraph), (2, table)):
            ranges = sorted(
                (ref["text_start"], ref["text_end"], ref["source_start"], ref["source_end"])
                for row in batches.rows for ref in row.get("chunk_refs", [])
                if ref["chunk_index"] == index
            )
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], len(text))
            self.assertTrue(all(left[1] == right[0] for left, right in zip(ranges, ranges[1:])))
            self.assertTrue(all(item[2] == item[0] and item[3] == item[1] for item in ranges))
        self.assertFalse(any(ref["page_number"] == 3 for row in batches.rows for ref in row.get("chunk_refs", [])))

    def test_five_text_pages_are_all_enqueued_as_ai_work(self):
        analysis = {"_id": "five-pages", "user_id": "user", "tender_id": "tender", "file_hash": "hash",
                    "provider_policy_version": 1}
        page_texts = {page: f"Page {page}: bidder shall submit certificate and comply with condition {page}."
                      for page in range(1, 6)}
        chunks = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash",
             "chunk_index": page, "page_number": page,
             "text": text, "char_start": 0, "char_end": len(text)}
            for page, text in page_texts.items()
        ])
        batches = MemoryCollection()
        total, _ = service._plan_batches(analysis, batches, chunks, page_count=5)
        enqueued_pages = {ref["page_number"] for batch in batches.rows for ref in batch.get("chunk_refs", [])}
        self.assertGreater(total, 0)
        self.assertEqual(enqueued_pages, {1, 2, 3, 4, 5})
        pages = MemoryCollection([{"user_id": "user", "tender_id": "tender", "file_hash": "hash",
                                   "page_number": page, "part_index": 0, "complete": True, "text": text}
                                  for page, text in page_texts.items()])
        response = SimpleNamespace(text='{"overview": {}, "requirements": []}',
                                    candidates=[SimpleNamespace(finish_reason="STOP")],
                                    usage_metadata=SimpleNamespace())
        analyses = MemoryCollection([dict(analysis, processing_token="lease")])
        captured = []
        def provider(messages, *_args):
            captured.append(messages[1]["content"])
            return response, 200, 100, 100, None, 200, 1
        with patch.object(service, "_provider_request", side_effect=provider), \
                patch.object(service, "_save_requirements", return_value=0), \
                patch.object(service, "_validated_overview_fields", return_value={}):
            for _ in range(total):
                self.assertTrue(service._process_batch(analyses.rows[0], "lease", analyses, batches,
                                                       MemoryCollection(), pages, chunks))
        sent_pages = {page for content in captured for page in range(1, 6)
                      if f"[SOURCE PAGE {page}" in content}
        self.assertEqual(sent_pages, {1, 2, 3, 4, 5})

    def test_batch_claim_honors_retry_deadline_and_terminal_provider_failure(self):
        now = service._now()
        analysis = {"_id": "analysis", "provider_policy_version": 1,
                    "provider_order": ["groq", "openrouter", "gemini"]}
        batches = MemoryCollection([
            {"_id": "future", "analysis_id": "analysis", "status": "retrying",
             "retry_at": now + timedelta(minutes=1), "provider_window_attempt_count": 1},
            {"_id": "terminal", "analysis_id": "analysis", "status": "failed",
             "terminal_failure": True, "provider_window_attempt_count": 1},
            {"_id": "due", "analysis_id": "analysis", "status": "retrying",
             "retry_at": now - timedelta(seconds=1), "provider_window_attempt_count": 1},
        ])
        claimed = batches.find_one_and_update(service._batch_claim_query(analysis, now, 3),
                                               {"$set": {"status": "processing"}})
        self.assertEqual(claimed["_id"], "due")
        self.assertEqual(batches.find_one({"_id": "future"})["status"], "retrying")
        self.assertEqual(batches.find_one({"_id": "terminal"})["status"], "failed")

    def test_advance_drains_all_claimable_batches_in_one_call(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender",
                    "file_hash": "hash", "status": "pending", "plan_ready": True,
                    "total_batches": 2, "lease_until": None}
        analyses = MemoryCollection([dict(analysis)])
        batches = MemoryCollection([
            {"_id": "first", "analysis_id": "analysis", "status": "pending", "batch_index": 0},
            {"_id": "second", "analysis_id": "analysis", "status": "pending", "batch_index": 1},
        ])
        with patch.object(service, "_process_batch", side_effect=[True, True, False]) as process, \
             patch.object(service, "_refresh_counts"):
            service._advance(analyses.rows[0], analyses, batches, MemoryCollection(), MemoryCollection(), MemoryCollection())
        self.assertEqual(process.call_count, 3)

    def test_advance_processes_independent_chunks_with_bounded_concurrency(self):
        analysis = {"_id": "parallel", "user_id": "user", "tender_id": "tender",
                    "file_hash": "hash", "status": "pending", "plan_ready": True,
                    "total_batches": 3, "lease_until": None}
        analyses = MemoryCollection([dict(analysis)])
        batches = MemoryCollection()
        gate = threading.Barrier(3)
        guard = threading.Lock()
        state = {"calls": 0, "active": 0, "max_active": 0}

        def process(*_args):
            with guard:
                state["calls"] += 1
                call = state["calls"]
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                if call <= 3:
                    gate.wait(timeout=3)
                    return True
                return False
            finally:
                with guard:
                    state["active"] -= 1

        with patch.dict(service.current_app.config, {"ANALYSIS_MAX_CONCURRENT_REQUESTS": 3}), \
                patch.object(service, "_process_batch", side_effect=process), \
                patch.object(service, "_refresh_counts"):
            service._advance(analyses.rows[0], analyses, batches, MemoryCollection(),
                            MemoryCollection(), MemoryCollection())
        self.assertEqual(state["max_active"], 3)
        self.assertEqual(state["calls"], 6)

    def test_parallel_chunk_failure_does_not_stop_other_workers(self):
        analysis = {"_id": "parallel-failure", "user_id": "user", "tender_id": "tender",
                    "file_hash": "hash", "status": "pending", "plan_ready": True,
                    "total_batches": 3, "lease_until": None}
        analyses = MemoryCollection([dict(analysis)])
        gate = threading.Barrier(3)
        guard = threading.Lock()
        calls = {"count": 0}

        def process(*_args):
            with guard:
                calls["count"] += 1
                call = calls["count"]
            if call <= 3:
                gate.wait(timeout=3)
                if call == 1:
                    raise service.AnalysisError("one independent chunk failed", 502)
                return True
            return False

        with patch.dict(service.current_app.config, {"ANALYSIS_MAX_CONCURRENT_REQUESTS": 3}), \
                patch.object(service, "_process_batch", side_effect=process), \
                patch.object(service, "_refresh_counts"):
            service._advance(analyses.rows[0], analyses, MemoryCollection(), MemoryCollection(),
                             MemoryCollection(), MemoryCollection())
        self.assertEqual(calls["count"], 5)

    def test_parallel_finding_persistence_serializes_duplicate_merges(self):
        guard = threading.Lock()
        state = {"active": 0, "max_active": 0, "calls": 0}

        def persist(*_args, **_kwargs):
            with guard:
                state["calls"] += 1
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            threading.Event().wait(0.02)
            with guard:
                state["active"] -= 1
            return 0

        with patch.object(service, "_save_requirements_unlocked", side_effect=persist):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(service._save_requirements, [], {}, {}, None, None)
                           for _ in range(2)]
                self.assertEqual([future.result() for future in futures], [0, 0])
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1)

    def _fresh_run_collections(self, status="needs_review"):
        user_id, tender_id, file_hash = "user", "tender", "hash"
        old_created = service._now() - service.timedelta(days=1)
        old_analysis = {"_id": "old-run", "user_id": user_id, "tender_id": tender_id, "file_hash": file_hash,
                        "analysis_version": "5", "analysis_logic_version": "5", "prompt_version": "5",
                        "status": status, "created_at": old_created, "updated_at": old_created,
                        "page_count": 11, "extracted_pages": 11, "overview_parts": [], "overview_fields": {},
                        "requirements_count": 1, "requirement_count": 1, "verified_requirement_count": 1,
                        "review_requirement_count": 0, "phase7_eligible_count": 1,
                        "processing_started_at": old_created, "processing_finished_at": old_created,
                        "total_batches": 3, "completed_batches": 3, "pending_batches": 0, "failed_batches": 0}
        analyses = MemoryCollection([old_analysis])
        batches = MemoryCollection([{"_id": "old-batch", "analysis_id": "old-run", "batch_index": 0, "status": "completed"}])
        requirements = MemoryCollection([{"_id": "old-finding", "analysis_id": "old-run", "requirement_title": "Previous finding"}])
        extractions = MemoryCollection([{"user_id": user_id, "tender_id": tender_id, "file_hash": file_hash,
                                         "status": "completed", "page_count": 11, "extracted_pages": 11}])
        pages, chunks = MemoryCollection(), MemoryCollection()
        return analyses, batches, requirements, extractions, pages, chunks

    def test_fresh_analysis_creates_isolated_run_and_is_idempotent(self):
        user = {"_id": "user"}
        tender = {"_id": "tender", "sha256": "hash"}
        collections = self._fresh_run_collections()
        analyses, batches, requirements, _, _, _ = collections
        with patch.object(service, "_find_owned_tender", return_value=tender), \
                patch.object(service, "_ready_extraction", return_value={"page_count": 11, "extracted_pages": 11}), \
                patch.object(service, "_collections", return_value=collections), \
                patch.object(service, "_advance", return_value={"status": "processing", "analysis_version": "8"}) as advance:
            first, started = service.start_fresh_analysis(user, "tender", request_id="fresh-request-1")
            second, duplicate_started = service.start_fresh_analysis(user, "tender", request_id="fresh-request-1")
        self.assertTrue(started)
        self.assertFalse(duplicate_started)
        self.assertEqual(first["analysis_version"], "8")
        self.assertEqual(second["analysis_version"], "8")
        self.assertEqual(analyses.rows[0]["status"], "needs_review")
        self.assertEqual(analyses.rows[1]["analysis_logic_version"], service.ANALYSIS_VERSION)
        self.assertEqual(analyses.rows[1]["prompt_version"], service.PROMPT_VERSION)
        self.assertEqual(analyses.rows[1]["run_origin"], "fresh")
        self.assertNotEqual(analyses.rows[0]["_id"], analyses.rows[1]["_id"])
        self.assertEqual(requirements.rows[0]["analysis_id"], "old-run")
        self.assertEqual([row["analysis_id"] for row in batches.rows], ["old-run"])
        advance.assert_called_once()

    def test_fresh_analysis_is_blocked_by_an_active_run(self):
        user = {"_id": "user"}
        tender = {"_id": "tender", "sha256": "hash"}
        collections = self._fresh_run_collections(status="processing")
        analyses = collections[0]
        analyses.rows[0]["lease_until"] = service._now() + service.timedelta(minutes=2)
        with patch.object(service, "_find_owned_tender", return_value=tender), \
                patch.object(service, "_ready_extraction", return_value={"page_count": 11, "extracted_pages": 11}), \
                patch.object(service, "_collections", return_value=collections), \
                patch.object(service, "_advance") as advance:
            result, started = service.start_fresh_analysis(user, "tender", request_id="another-request")
        self.assertFalse(started)
        self.assertTrue(result["in_progress"])
        self.assertEqual(len(analyses.rows), 1)
        advance.assert_not_called()

    def test_standard_start_and_resume_continue_the_latest_run(self):
        user = {"_id": "user"}
        tender = {"_id": "tender", "sha256": "hash"}
        collections = self._fresh_run_collections()
        latest = dict(collections[0].rows[0], _id="fresh-run", analysis_version="7",
                      analysis_logic_version="7", run_origin="fresh", status="paused_rate_limit")
        collections[0].rows.append(latest)
        with patch.object(service, "_find_owned_tender", return_value=tender), \
                patch.object(service, "_ready_extraction", return_value={"page_count": 11, "extracted_pages": 11}), \
                patch.object(service, "_collections", return_value=collections), \
                patch.object(service, "_advance", return_value={"status": "paused_rate_limit", "analysis_version": "7"}) as advance:
            started, created = service.start_analysis(user, "tender")
            resumed = service.resume_analysis(user, "tender")
        self.assertFalse(created)
        self.assertEqual(started["analysis_version"], "7")
        self.assertEqual(resumed["analysis_version"], "7")
        self.assertEqual(advance.call_count, 1)
        self.assertEqual(advance.call_args.args[0]["_id"], "fresh-run")

    def test_single_reference_truncation_splits_to_text_ranges_idempotently(self):
        batch = {"_id": "single", "analysis_id": "analysis", "user_id": "user", "tender_id": "tender",
                 "file_hash": "hash", "batch_index": 0, "chunk_refs": [{"chunk_index": 1, "page_number": 1}],
                 "status": "split_pending"}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([{"_id": "analysis"}])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": "0123456789",
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        batch["analysis_work_policy"] = {"max_split_depth": 2, "min_segment_chars": 1}
        batches.rows[0]["analysis_work_policy"] = batch["analysis_work_policy"]
        self.assertEqual(service._split_truncated_batch(batch, batches, chunks, analyses), 2)
        self.assertEqual(service._split_truncated_batch(batches.rows[0], batches, chunks, analyses), 2)
        self.assertEqual(len(batches.rows), 3)
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual(sorted((row["chunk_refs"][0]["text_start"], row["chunk_refs"][0]["text_end"])
                                for row in batches.rows[1:]), [(0, 5), (5, 10)])

    def test_truncation_remains_recoverable_past_the_old_two_level_limit(self):
        text = "Requirement text. " * 300
        batch = {"_id": "deep-parent", "analysis_id": "analysis", "user_id": "user",
                 "tender_id": "tender", "file_hash": "hash", "batch_index": 0,
                 "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 0,
                                 "text_end": len(text)}], "status": "split_pending",
                 "subdivision_depth": 2, "analysis_work_policy": {"max_split_depth": 5,
                                                                      "min_segment_chars": 120}}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([{"_id": "analysis"}])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": text,
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        self.assertEqual(service._split_truncated_batch(batch, batches, chunks, analyses), 2)
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual({row["subdivision_depth"] for row in batches.rows if row.get("parent_batch_id")}, {3})

    def test_single_reference_subdivision_is_bounded_to_two_child_levels(self):
        text = "x" * 1024
        batch = {"_id": "root", "analysis_id": "analysis", "user_id": "user", "tender_id": "tender",
                 "file_hash": "hash", "batch_index": 0, "status": "split_pending",
                 "analysis_work_policy": {"max_split_depth": 2, "min_segment_chars": 100},
                 "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 0, "text_end": len(text),
                                 "source_start": 500, "source_end": 500 + len(text)}]}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([{"_id": "analysis"}])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": text, "char_start": 500,
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        service._split_truncated_batch(batches.rows[0], batches, chunks, analyses)
        level_one = [row for row in batches.rows if row.get("parent_batch_id") == "root"]
        self.assertEqual(len(level_one), 2)
        for child in level_one:
            service._split_truncated_batch(child, batches, chunks, analyses)
        level_two = [row for row in batches.rows if row.get("parent_batch_id") in {row["_id"] for row in level_one}]
        self.assertEqual(len(level_two), 4)
        ranges = []
        for leaf in level_two:
            self.assertEqual(leaf["subdivision_depth"], 2)
            service._split_truncated_batch(leaf, batches, chunks, analyses)
            self.assertEqual(leaf["status"], "skipped")
            ref = leaf["chunk_refs"][0]
            ranges.append((ref["text_start"], ref["text_end"]))
        self.assertEqual(sorted(ranges), [(0, 256), (256, 512), (512, 768), (768, 1024)])

    def test_single_reference_split_uses_tender_scoped_source_chunk(self):
        batch = {"_id": "scoped", "analysis_id": "analysis", "user_id": "u1", "tender_id": "t1",
                 "file_hash": "h1", "batch_index": 0, "chunk_refs": [{"chunk_index": 7, "page_number": 2}],
                 "status": "split_pending", "analysis_work_policy": {"max_split_depth": 2, "min_segment_chars": 1}}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([{"_id": "analysis"}])
        chunks = MemoryCollection([
            {"chunk_index": 7, "page_number": 2, "text": "abcdefghij", "user_id": "u1", "tender_id": "t1", "file_hash": "h1"},
            {"chunk_index": 7, "page_number": 2, "text": "x" * 100, "user_id": "u2", "tender_id": "t2", "file_hash": "h2"},
        ])
        service._split_truncated_batch(batch, batches, chunks, analyses)
        children = [row for row in batches.rows if row.get("parent_batch_id") == "scoped"]
        self.assertEqual(sorted(ref["text_end"] for row in children for ref in row["chunk_refs"]), [5, 10])

    def test_concurrent_splits_reserve_unique_indexes_and_same_parent_is_idempotent(self):
        parents = [{"_id": f"parent-{i}", "analysis_id": "analysis", "user_id": "user",
                    "tender_id": "tender", "file_hash": "hash", "batch_index": i,
                    "status": "split_pending", "chunk_refs": [
                        {"chunk_index": i * 2 + 1, "page_number": i * 2 + 1},
                        {"chunk_index": i * 2 + 2, "page_number": i * 2 + 2},
                    ]} for i in range(2)]
        batches = MemoryCollection(parents)
        batches.enforce_batch_unique = True
        analyses = MemoryCollection([{"_id": "analysis"}])
        chunks = MemoryCollection([{"chunk_index": i, "page_number": i, "text": f"page {i}",
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
                                   for i in range(1, 5)])
        def split(parent):
            with self.app.app_context():
                return service._split_truncated_batch(parent, batches, chunks, analyses)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(split, [parents[0], parents[1]] * 4))
        indexes = [row["batch_index"] for row in batches.rows]
        self.assertEqual(len(indexes), len(set(indexes)))
        for parent in parents:
            children = [row for row in batches.rows if row.get("parent_batch_id") == parent["_id"]]
            self.assertEqual(len(children), 2)
            self.assertEqual(batches.find_one({"_id": parent["_id"]})["status"], "split")

    def test_resume_recovers_partial_split_without_duplicate_children_or_findings(self):
        parent = {"_id": "resume-parent", "analysis_id": "analysis", "user_id": "user",
                  "tender_id": "tender", "file_hash": "hash", "batch_index": 0,
                  "status": "split_pending", "chunk_refs": [
                      {"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}],
                  "findings_persisted": True, "split_plan": [
                      {"_id": "existing-child", "batch_index": 8,
                       "chunk_refs": [{"chunk_index": 1, "page_number": 1}]},
                      {"_id": "missing-child", "batch_index": 9,
                       "chunk_refs": [{"chunk_index": 2, "page_number": 2}]},
                  ]}
        child = {"_id": "existing-child", "analysis_id": "analysis", "parent_batch_id": "resume-parent",
                 "batch_index": 8, "status": "completed", "findings": ["kept"]}
        batches = MemoryCollection([parent, child])
        batches.enforce_batch_unique = True
        analyses = MemoryCollection([{"_id": "analysis", "next_batch_index": 10}])
        self.assertEqual(service._split_truncated_batch(parent, batches, analyses=analyses), 2)
        self.assertEqual(service._split_truncated_batch(parent, batches, analyses=analyses), 2)
        children = [row for row in batches.rows if row.get("parent_batch_id") == "resume-parent"]
        self.assertEqual(len(children), 2)
        self.assertEqual(batches.find_one({"_id": "existing-child"})["findings"], ["kept"])
        self.assertEqual(len([row for row in batches.rows if row.get("_id") == "missing-child"]), 1)

    def test_out_of_range_saved_segment_is_skipped_without_provider_call(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"_id": "bad-range", "analysis_id": "analysis", "user_id": "user", "tender_id": "tender",
                 "file_hash": "hash", "batch_index": 0, "status": "pending", "attempt_count": 0,
                 "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 20, "text_end": 40}]}
        analyses = MemoryCollection([dict(analysis, processing_token="lease")])
        batches = MemoryCollection([batch])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": "short text",
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        with patch.object(service, "_provider_request") as provider:
            self.assertTrue(service._process_batch(analysis, "lease", analyses, batches,
                                                   MemoryCollection(), MemoryCollection(), chunks))
        self.assertEqual(batches.rows[0]["status"], "skipped")
        self.assertIn("outside", batches.rows[0]["skipped_reason"])
        provider.assert_not_called()

    def test_smallest_segment_is_skipped_at_subdivision_limit(self):
        batch = {"_id": "tiny", "analysis_id": "analysis", "user_id": "user", "tender_id": "tender",
                 "file_hash": "hash", "batch_index": 0, "subdivision_depth": service.MAX_TEXT_SUBDIVISION_DEPTH,
                 "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 0, "text_end": 2}],
                 "status": "split_pending"}
        batches = MemoryCollection([batch])
        analyses = MemoryCollection([{"_id": "analysis"}])
        self.assertEqual(service._split_truncated_batch(batch, batches, analyses=analyses), 0)
        self.assertEqual(batches.rows[0]["status"], "skipped")
        self.assertEqual(batches.rows[0]["skipped_text_ranges"][0]["text_end"], 2)

    def test_fallback_cooldown_is_persisted_and_resume_waits_until_expiry(self):
        analysis = {"_id": "analysis", "status": "pending", "plan_ready": True, "total_batches": 1,
                    "active_started_at": service._now(), "coverage": {}}
        analyses = MemoryCollection([analysis])
        batches = MemoryCollection([{"_id": "batch", "analysis_id": "analysis", "status": "processing"}])
        requirements = MemoryCollection()
        pause = service.AnalysisPaused("temporary", retry_after=60)
        with patch.object(service, "_process_batch", side_effect=pause) as process:
            first = service._advance(analyses.rows[0], analyses, batches, requirements, MemoryCollection(), MemoryCollection())
            saved_until = analyses.rows[0]["pause_until"]
            self.assertEqual(first["status"], "paused_rate_limit")
            self.assertEqual(analyses.rows[0]["pause_reason"], "rate_limit")
            self.assertGreater(saved_until, service._now())
            second = service._advance(analyses.rows[0], analyses, batches, requirements, MemoryCollection(), MemoryCollection())
        self.assertEqual(second["status"], "paused_rate_limit")
        process.assert_called_once()

    def test_daily_quota_pause_is_persisted_with_separate_reason(self):
        analysis = {"_id": "analysis", "status": "pending", "plan_ready": True, "total_batches": 1,
                    "active_started_at": service._now(), "coverage": {}}
        analyses = MemoryCollection([analysis])
        batches = MemoryCollection([{"_id": "batch", "analysis_id": "analysis", "status": "processing"}])
        with patch.object(service, "_process_batch", side_effect=service.AnalysisPaused("quota", daily_quota=True, retry_after=86400)):
            result = service._advance(analyses.rows[0], analyses, batches, MemoryCollection(), MemoryCollection(), MemoryCollection())
        self.assertEqual(result["status"], "paused_rate_limit")
        self.assertEqual(analyses.rows[0]["pause_reason"], "daily_quota")
        self.assertGreater(analyses.rows[0]["pause_until"], service._now())

    def test_daily_quota_cooldown_is_long_and_tagged_separately(self):
        error = self._rate_error(body={"error": {"message": "Daily quota exceeded"}})
        with patch.dict(service.current_app.config, {"ANALYSIS_DAILY_QUOTA_FALLBACK_SECONDS": 72000}):
            self.assertEqual(service._rate_limit_cooldown(error, daily_quota=True), 72000)

    def test_retry_attempt_usage_counts_failed_and_successful_attempts(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        client.models.generate_content.side_effect = self._rate_error()
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"), \
                patch.object(service.time, "sleep"):
            with self.assertRaises(service.AnalysisPaused):
                service._process_batch(analysis, "lease", analyses, batches, MemoryCollection(), pages, chunks)
            batches.rows[0]["status"] = "pending"  # Simulate a safe later resume after cooldown.
            client.models.generate_content.side_effect = None
            client.models.generate_content.return_value = result
            service._process_batch(analysis, "lease", analyses, batches, MemoryCollection(), pages, chunks)
        saved = analyses.rows[0]
        self.assertEqual(saved["request_count"], 2)
        self.assertEqual(saved["unknown_usage_requests"], 1)
        self.assertEqual(saved["actual_input_tokens"], 44)
        self.assertEqual(saved["actual_output_tokens"], 12)
        self.assertEqual(saved["prompt_versions_used"], [service.PROMPT_VERSION])

    def test_evidence_requires_contiguous_text_unless_ellipsis_is_explicit(self):
        source = "THE bidder shall, SUBMIT its valid certificate."
        self.assertTrue(service._evidence_supported("the BIDDER shall submit, its valid certificate!", source))
        self.assertFalse(service._evidence_supported("bidder submit valid certificate", source))
        self.assertTrue(service._evidence_supported("bidder shall... valid certificate", source))
        self.assertFalse(service._evidence_supported("valid certificate... bidder shall", source))

    def test_page_evidence_lookup_preserves_user_and_tender_ownership(self):
        pages = MemoryCollection([{"user_id": "other-user", "tender_id": "other-tender", "file_hash": "hash",
                                   "page_number": 1, "part_index": 0, "complete": True,
                                   "text": "The bidder shall submit certificate."}])
        matches = service._page_evidence_matches(
            pages, "user", "tender", "hash", 1, {"The bidder shall submit certificate."}
        )
        self.assertEqual(matches, set())

    def _raw_requirement(self, page, evidence, title="Similar equipment experience", value="3 years"):
        return {"category": "Experience Requirements", "requirement_title": title,
                "description": "The bidder must have supplied similar equipment for 3 years",
                "requirement_value": value, "mandatory": True, "source_page": page,
                "evidence_text": evidence, "extraction_status": "extracted"}

    def test_duplicate_requirements_consolidate_and_preserve_pages_without_downgrade(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0, "chunk_refs": [{"page_number": 1}, {"page_number": 2}]}
        pages = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 1, "part_index": 0, "complete": True,
             "text": "The bidder must have supplied similar equipment for 3 years."},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 2, "part_index": 0, "complete": True,
             "text": "The bidder must have supplied similar equipment for 3 years."},
        ])
        requirements = MemoryCollection()
        raw = [
            self._raw_requirement(1, "The bidder must have supplied similar equipment for 3 years."),
            self._raw_requirement(2, "The bidder must have supplied similar equipment for 3 years."),
        ]
        raw[1]["mandatory"] = None
        service._save_requirements(raw, analysis, batch, pages, requirements)
        self.assertEqual(len(requirements.rows), 1)
        saved = requirements.rows[0]
        self.assertEqual(saved["validation_status"], "verified")
        self.assertEqual({source["source_page"] for source in saved["source_evidence"]}, {1, 2})
        uncertain_repeat = self._raw_requirement(2, "unsupported excerpt", title="Similar equipment experience")
        service._save_requirements([uncertain_repeat], analysis,
                                   {"batch_index": 1, "chunk_refs": [{"page_number": 2}]}, pages, requirements)
        self.assertEqual(requirements.rows[0]["validation_status"], "verified")

    def test_identical_retry_does_not_create_duplicate_findings(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0, "chunk_refs": [{"page_number": 1}]}
        pages = MemoryCollection([{"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 1, "part_index": 0, "complete": True,
                                   "text": "The bidder must have supplied similar equipment for 3 years."}])
        requirements = MemoryCollection()
        raw = [self._raw_requirement(1, "The bidder must have supplied similar equipment for 3 years.")]
        service._save_requirements(raw, analysis, batch, pages, requirements)
        service._save_requirements(raw, analysis, batch, pages, requirements)
        self.assertEqual(len(requirements.rows), 1)
        self.assertEqual(len(requirements.rows[0]["source_evidence"]), 1)

    def test_bulk_write_failure_preserves_original_mongo_exception_and_logs_context(self):
        class FailingRequirements(MemoryCollection):
            def bulk_write(self, operations, ordered=False):
                raise OperationFailure("duplicate key on requirement_key", code=11000)

        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"_id": "batch-7", "batch_index": 7, "chunk_refs": [{"page_number": 1}]}
        evidence = "The bidder must have supplied similar equipment for 3 years."
        pages = MemoryCollection([{
            "user_id": "user", "tender_id": "tender", "file_hash": "hash",
            "page_number": 1, "part_index": 0, "complete": True, "text": evidence,
        }])
        with patch.object(service._logger, "exception") as log:
            with self.assertRaises(OperationFailure) as caught:
                service._save_requirements(
                    [self._raw_requirement(1, evidence)], analysis, batch, pages, FailingRequirements()
                )
        self.assertIn("duplicate key", str(caught.exception))
        log.assert_called_once()
        self.assertEqual(log.call_args.args[0], "Agent 1 MongoDB write failed")
        self.assertEqual(log.call_args.kwargs["extra"]["operation"], "requirements.bulk_write")
        self.assertEqual(log.call_args.kwargs["extra"]["batch_id"], "batch-7")
        self.assertEqual(log.call_args.kwargs["extra"]["batch_index"], 7)
        self.assertEqual(log.call_args.kwargs["extra"]["exception_type"], "OperationFailure")

    def test_bulk_write_diagnostic_is_persisted_on_analysis_and_batch(self):
        class FailingRequirements(MemoryCollection):
            def bulk_write(self, operations, ordered=False):
                raise OperationFailure("duplicate key on requirement_key", code=11000)

        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content='{"overview": {}, "requirements": [{"category": "Experience Requirements", '
                        '"requirement_title": "Experience", "description": "The bidder must have supplied similar equipment for 3 years", '
                        '"requirement_value": "3 years", "mandatory": true, "source_page": 1, '
                        '"evidence_text": "The bidder must have supplied similar equipment for 3 years."}]}'))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        analysis.update({"plan_ready": True, "status": "pending", "total_batches": 1})
        analyses = MemoryCollection([dict(analysis)])
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"), \
                patch.object(service._logger, "exception"):
            response = service._advance(
                analyses.rows[0], analyses, batches, FailingRequirements(), pages, chunks
            )
        self.assertEqual(response["status"], "failed")
        diagnostic = analyses.rows[0]["mongo_diagnostic"]
        self.assertEqual(diagnostic["operation"], "requirements.bulk_write")
        self.assertEqual(diagnostic["batch_id"], "batch")
        self.assertEqual(diagnostic["exception_type"], "OperationFailure")
        self.assertIn("duplicate key", diagnostic["exception_message"])
        self.assertEqual(batches.rows[0]["mongo_diagnostic"], diagnostic)

    def test_original_write_error_survives_failed_status_write(self):
        class FailingRequirements(MemoryCollection):
            def bulk_write(self, operations, ordered=False):
                raise OperationFailure("duplicate key on requirement_key", code=11000)

        class FailingStatusBatches(MemoryCollection):
            def update_one(self, query, update, upsert=False):
                if update.get("$set", {}).get("status") == "failed":
                    raise RuntimeError("status update unavailable")
                return super().update_one(query, update, upsert)

        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content='{"overview": {}, "requirements": [{"category": "Experience Requirements", '
                        '"requirement_title": "Experience", "description": "The bidder must have supplied similar equipment for 3 years", '
                        '"requirement_value": "3 years", "mandatory": true, "source_page": 1, '
                        '"evidence_text": "The bidder must have supplied similar equipment for 3 years."}]}'))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, _, pages, chunks, client = self._batch_inputs(result)
        batches = FailingStatusBatches([batch])
        messages = []
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"), \
                patch.object(service._logger, "exception", side_effect=lambda message, **kwargs: messages.append(message)):
            with self.assertRaises(OperationFailure) as caught:
                service._process_batch(
                    analysis, "lease", analyses, batches, FailingRequirements(), pages, chunks
                )
        self.assertIn("duplicate key", str(caught.exception))
        self.assertIn("Agent 1 MongoDB write failed", messages)
        self.assertIn("Agent 1 failed to persist batch failure status", messages)

    def test_legacy_preliminary_conflict_failed_batch_gets_one_safe_resume(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=44, completion_tokens=12),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        batches.rows[0].update({
            "status": "failed", "attempt_count": 3,
            "error_message": "batch op errors occurred, code': 40, Updating the path 'preliminary'",
        })
        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            service._process_batch(analysis, "lease", analyses, batches, MemoryCollection(), pages, chunks)
        self.assertEqual(batches.rows[0]["status"], "completed")
        self.assertTrue(batches.rows[0]["legacy_retry_used"])

    def test_summary_exposes_redacted_safe_diagnostics(self):
        analysis = {
            "_id": "analysis", "mongo_diagnostic": {
                "operation": "requirements.bulk_write", "batch_id": "batch-1", "batch_index": 1,
                "exception_type": "OperationFailure",
                "exception_message": "mongodb+srv://user:secret@example/db duplicate key",
                "error_code": 11000, "timestamp": service._now(),
            },
        }
        summary = service._summary(analysis)
        self.assertEqual(summary["mongo_diagnostic"]["error_code"], 11000)
        self.assertNotIn("secret", summary["mongo_diagnostic"]["exception_message"])
        self.assertEqual(summary["mongo_diagnostic"]["operation"], "requirements.bulk_write")

    def test_preliminary_and_final_upserts_do_not_overlap_update_operators(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        evidence = "The bidder must have supplied similar equipment for 3 years."
        batch = {"batch_index": 0, "chunk_refs": [{"page_number": 1}]}
        pages = MemoryCollection([{
            "user_id": "user", "tender_id": "tender", "file_hash": "hash",
            "page_number": 1, "part_index": 0, "complete": True, "text": evidence,
        }])
        requirements = MemoryCollection()
        raw = [self._raw_requirement(1, evidence)]

        service._save_requirements(raw, analysis, batch, pages, requirements, preliminary=True)
        preliminary_update = requirements.updates[-1][1]
        self.assertEqual(preliminary_update["$setOnInsert"]["preliminary"], True)
        self.assertNotIn("preliminary", preliminary_update.get("$set", {}))
        self.assertTrue(set(preliminary_update.get("$setOnInsert", {})).isdisjoint(
            preliminary_update.get("$set", {})
        ))

        service._save_requirements(raw, analysis, batch, pages, requirements, preliminary=False)
        final_update = requirements.updates[-1][1]
        self.assertNotIn("preliminary", final_update.get("$setOnInsert", {}))
        self.assertEqual(final_update["$set"]["preliminary"], False)
        self.assertTrue(set(final_update.get("$setOnInsert", {})).isdisjoint(
            final_update.get("$set", {})
        ))

    def test_distinct_values_remain_separate_and_conflicted_for_review(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0, "chunk_refs": [{"page_number": 1}, {"page_number": 2}]}
        pages = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 1, "part_index": 0, "complete": True, "text": "Bidder must have 3 years similar equipment experience."},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 2, "part_index": 0, "complete": True, "text": "Bidder must have 5 years similar equipment experience."},
        ])
        requirements = MemoryCollection()
        raw = [
            self._raw_requirement(1, "Bidder must have 3 years similar equipment experience.", value="3 years"),
            self._raw_requirement(2, "Bidder must have 5 years similar equipment experience.", value="5 years"),
        ]
        service._save_requirements(raw, analysis, batch, pages, requirements)
        self.assertEqual(len(requirements.rows), 2)
        self.assertTrue(all(row["validation_status"] == "needs_review" for row in requirements.rows))

    def test_bidder_and_product_specification_classification(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0}
        bidder_evidence = "The bidder shall have valid prior supply experience."
        bidder = service._validate_requirement(
            {"category": "Eligibility Requirements", "requirement_title": "Prior experience",
             "description": bidder_evidence, "requirement_value": "", "mandatory": True,
             "source_page": 1, "evidence_text": bidder_evidence, "extraction_status": "extracted"},
            {1}, analysis, batch, {1: {bidder_evidence}}, 0,
        )
        product_evidence = "The pump shall provide 100 litres per minute flow."
        product = service._validate_requirement(
            {"category": "Product and Equipment Specifications", "requirement_title": "Pump flow",
             "description": product_evidence, "requirement_value": "100 litres per minute",
             "mandatory": True, "source_page": 1, "evidence_text": product_evidence,
             "extraction_status": "extracted"},
            {1}, analysis, batch, {1: {product_evidence}}, 1,
        )
        self.assertEqual(bidder["category"], "Experience Requirements")
        self.assertEqual(bidder["validation_status"], "verified")
        self.assertTrue(bidder["eligible_for_phase7"])
        self.assertEqual(product["validation_status"], "verified")
        self.assertFalse(product["eligible_for_phase7"])
        unsupported = service._validate_requirement(
            {"category": "Eligibility Requirements", "requirement_title": "Prior experience",
             "description": bidder_evidence, "requirement_value": "", "mandatory": True,
             "source_page": 1, "evidence_text": "The bidder holds a certificate.", "extraction_status": "extracted"},
            {1}, analysis, batch, {1: set()}, 2,
        )
        self.assertEqual(unsupported["validation_status"], "needs_review")
        self.assertFalse(unsupported["eligible_for_phase7"])

    def test_evidence_accepts_supported_paraphrase_but_rejects_changed_numbers_dates_and_assumptions(self):
        source = "Bids should have a validity of 90 days and be submitted on or before 29th August 2024."
        self.assertTrue(service._claim_supported("Bids remain valid for 90 days", source))
        self.assertFalse(service._claim_supported("Bids remain valid for 120 days", source))
        self.assertFalse(service._claim_supported("Submission closes on 29th August 2025", source))
        self.assertFalse(service._claim_supported("The bidder automatically qualifies for award", source))

    def test_page_grounded_validation_accepts_paraphrase_and_reviews_unsupported_facts(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0}
        source = "Bids should have a validity of 90 days and be submitted on or before 29th August 2024."
        evidence = "Bids should have a validity of 90 days."
        base = {"category": "Important Dates and Deadlines", "requirement_title": "Bid validity",
                "description": "Bids remain valid for 90 days.", "requirement_value": "90 days",
                "mandatory": True, "source_page": 1, "evidence_text": evidence, "extraction_status": "extracted"}
        accepted = service._validate_requirement(base, {1}, analysis, batch, {1: {evidence}}, 0, {1: source})
        self.assertEqual(accepted["validation_status"], "verified")
        invented = dict(base, description="Bids remain valid for 120 days.", requirement_value="120 days")
        rejected = service._validate_requirement(invented, {1}, analysis, batch, {1: {evidence}}, 1, {1: source})
        self.assertEqual(rejected["validation_status"], "needs_review")
        self.assertFalse(rejected["source_evidence"][0]["verified"])

    def test_latest_iiitdm_examples_get_distinct_categories(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0}
        cases = [
            ("Financial Requirements", "Annual Turnover", "Bidder annual turnover should exceed Rs. 10 crore.", "Bidder annual turnover should exceed Rs. 10 crore."),
            ("Eligibility Requirements", "Local Supplier Classification", "Bidder should be a Class-I or Class-II local supplier.", "The bidder should be a Class-I / Class-II Local Supplier."),
            ("Technical Requirements", "Function Generator Frequency", "The function generator frequency range shall be 1 MHz to 20 MHz.", "Frequency range shall be 1 MHz to 20 MHz."),
            ("Eligibility Requirements", "Prior Supply Experience", "Bidder should have supplied two similar equipment on or after 1/1/2017.", "Should have supplied two similar equipment on or after 1/1/2017."),
            ("Eligibility Requirements", "OEM Authorization Letter", "The OEM authorization letter shall be submitted as Annexure-4.", "OEM authorization letter shall be submitted as Annexure-4."),
            ("Financial Requirements", "Payment Terms and Bank Guarantee", "100% payment after delivery, installation and acceptance against a 3% bank guarantee.", "100% payment after delivery, installation and acceptance against a 3% bank guarantee."),
            ("Other Mandatory Bidder Conditions", "Make in India Policy Compliance", "The institute follows the Public Procurement Preference to Make in India Order 2017.", "The institute follows the Public Procurement Preference to Make in India Order 2017."),
        ]
        expected = ["Financial Requirements", "Procurement Policies", "Product and Equipment Specifications",
                    "Experience Requirements", "Required Documents and Certificates", "Contractual Terms", "Procurement Policies"]
        for index, ((category, title, description, evidence), wanted) in enumerate(zip(cases, expected), 1):
            with self.subTest(title=title):
                item = service._validate_requirement(
                    {"category": category, "requirement_title": title, "description": description,
                     "requirement_value": "", "mandatory": True, "source_page": index,
                     "evidence_text": evidence, "extraction_status": "extracted"},
                    {index}, analysis, batch, {index: {evidence}}, index,
                )
                self.assertEqual(item["category"], wanted)

    def test_iiitdm_duplicate_requirements_merge_page_refs_but_conflicting_requirement_stays_separate(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        pages = MemoryCollection([
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 3, "part_index": 0, "complete": True,
             "text": "Experience Certificate: Should have Supplied Two similar equipment on or after 1/1/2017."},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 10, "part_index": 0, "complete": True,
             "text": "Should have supplied two comparable equipment on or after 1/1/2017."},
            {"user_id": "user", "tender_id": "tender", "file_hash": "hash", "page_number": 11, "part_index": 0, "complete": True,
             "text": "Bidder should have supplied three similar equipment on or after 1/1/2017."},
        ])
        requirements = MemoryCollection()
        raw = [
            {"category": "Experience Requirements", "requirement_title": "Prior Supply Experience",
             "description": "Bidder should supply two similar equipment on or after 1/1/2017.",
             "requirement_value": "Two similar equipment on or after 1/1/2017", "mandatory": True,
             "source_page": 3, "evidence_text": "Experience Certificate: Should have Supplied Two similar equipment on or after 1/1/2017.", "extraction_status": "extracted"},
            {"category": "Experience Requirements", "requirement_title": "Similar Equipment Supply Experience",
             "description": "Bidder should supply two comparable equipment on or after 1/1/2017.",
             "requirement_value": "Two comparable equipment on or after 1/1/2017", "mandatory": True,
             "source_page": 10, "evidence_text": "Should have supplied two comparable equipment on or after 1/1/2017.", "extraction_status": "extracted"},
            {"category": "Experience Requirements", "requirement_title": "Similar Equipment Supply Experience",
             "description": "Bidder should supply three similar equipment on or after 1/1/2017.",
             "requirement_value": "Three similar equipment on or after 1/1/2017", "mandatory": True,
             "source_page": 11, "evidence_text": "Bidder should have supplied three similar equipment on or after 1/1/2017.", "extraction_status": "extracted"},
        ]
        for batch_index, (page, item) in enumerate(zip((3, 10, 11), raw)):
            service._save_requirements([item], analysis,
                                       {"batch_index": batch_index, "chunk_refs": [{"page_number": page}]},
                                       pages, requirements)
        self.assertEqual(len(requirements.rows), 2)
        same_requirement = next(row for row in requirements.rows if "2" in str(row["material_anchors"]))
        self.assertEqual({row["source_page"] for row in same_requirement["source_evidence"]}, {3, 10})
        self.assertTrue(all(row["validation_status"] == "needs_review" for row in requirements.rows))

    def test_requirement_status_separates_conditional_optional_informational_and_mandatory(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        batch = {"batch_index": 0}
        cases = [
            ("Eligibility Requirements", "Local supplier exception", "If the bidder is an MSME, a valid certificate is required.", True, "conditional"),
            ("Required Documents and Certificates", "Optional brochure", "The bidder may submit an optional brochure.", False, "optional"),
            ("Other Mandatory Bidder Conditions", "Reference note", "For information only: the institute address is shown here.", None, "informational"),
            ("Eligibility Requirements", "Debarment", "The bidder shall not be debarred from government organizations.", True, "mandatory"),
        ]
        for page, (category, title, evidence, mandatory, wanted) in enumerate(cases, 1):
            with self.subTest(status=wanted):
                item = service._validate_requirement(
                    {"category": category, "requirement_title": title, "description": evidence,
                     "requirement_value": "", "mandatory": mandatory, "source_page": page,
                     "evidence_text": evidence, "extraction_status": "extracted"},
                    {page}, analysis, batch, {page: {evidence}}, page,
                )
                self.assertEqual(item["requirement_status"], wanted)
                if wanted == "conditional":
                    self.assertTrue(item["mandatory"])
                    self.assertFalse(item["unconditional_eligibility_blocker"])
                if wanted == "mandatory":
                    self.assertTrue(item["unconditional_eligibility_blocker"])

    def test_requirement_status_ignores_list_shaped_model_field(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        evidence = "The bidder shall submit the signed undertaking."

        item = service._validate_requirement(
            {"category": "Required Documents and Certificates", "requirement_title": "Signed undertaking",
             "description": evidence, "requirement_value": "", "mandatory": None,
             "requirement_status": ["mandatory"], "source_page": 1,
             "evidence_text": evidence, "extraction_status": "extracted"},
            {1}, analysis, {"batch_index": 0}, {1: {evidence}}, 0, {1: evidence},
        )

        self.assertEqual(item["requirement_status"], "mandatory")
        self.assertTrue(item["mandatory"])

    def test_gemini_output_token_truncation_and_task_splitting(self):
        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"requirements": ['))],
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=1400),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result)
        batch["chunk_refs"] = [{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}]
        batches.rows[0]["chunk_refs"] = list(batch["chunk_refs"])
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Page two requirement.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        pages.rows.append({"page_number": 2, "part_index": 0, "complete": True, "text": "Page two requirement."})
        requirements = MemoryCollection()

        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            worked = service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks)

        self.assertTrue(worked)
        self.assertEqual(batches.rows[0]["status"], "split")
        self.assertEqual(batches.rows[0]["finish_reason"], "MAX_TOKENS")
        self.assertIn("Truncated task was split into smaller resumable tasks", batches.rows[0]["error_message"])
        children = [r for r in batches.rows if r.get("parent_batch_id") == "batch"]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(c["status"] == "pending" for c in children))
        self.assertEqual({c["chunk_refs"][0]["page_number"] for c in children}, {1, 2})

    def test_truncated_response_does_not_create_findings_from_incomplete_json(self):
        # Recovered fragments remain reviewable and never complete their parent batch.
        partial_json = (
            '{"overview": {"tender_title": "Function Generator"}, '
            '"requirements": [{"category": "Technical Requirements", "requirement_title": "Frequency Range", '
            '"description": "Frequency range 1 MHz to 20 MHz", "requirement_value": "1 MHz to 20 MHz", '
            '"evidence_text": "The bidder shall submit certificate.", "source_page": 1}], '
            '"truncated_tail": {"bad": '
        )
        result1 = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=partial_json))],
            usage=SimpleNamespace(prompt_tokens=60, completion_tokens=1400),
        )
        analysis, batch, analyses, batches, pages, chunks, client = self._batch_inputs(result1)
        batch["chunk_refs"] = [{"chunk_index": 1, "page_number": 1}, {"chunk_index": 2, "page_number": 2}]
        batches.rows[0]["chunk_refs"] = list(batch["chunk_refs"])
        chunks.rows.append({"chunk_index": 2, "page_number": 2, "text": "Page two requirement.",
                            "user_id": "user", "tender_id": "tender", "file_hash": "hash"})
        pages.rows.append({"page_number": 2, "part_index": 0, "complete": True, "text": "Page two requirement."})
        requirements = MemoryCollection()

        with patch.object(service.genai, "Client", return_value=client), patch.object(service, "_pace"):
            service._process_batch(analysis, "lease", analyses, batches, requirements, pages, chunks)

        self.assertEqual(requirements.rows, [])
        self.assertEqual(batches.rows[0]["partial_requirement_count"], 0)
        self.assertNotEqual(batches.rows[0]["status"], "completed")

    def test_interrupted_batch_recovery(self):
        analysis = {"_id": "analysis", "status": "processing", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        # Interrupted batch with attempt_count = 1
        stale_time = service._now() - service.timedelta(seconds=400)
        batch = {"_id": "b1", "analysis_id": "analysis", "batch_index": 0, "status": "processing",
                 "attempt_count": 1, "last_activity_at": stale_time}
        batches = MemoryCollection([batch])

        service._recover_stale_batches("analysis", batches, lease_seconds=300, max_retries=3)

        # Should be recovered to pending for retry
        self.assertEqual(batches.rows[0]["status"], "pending")
        self.assertIn("Recovered for retry", batches.rows[0]["error_message"])
        self.assertEqual(batches.rows[0]["attempt_count"], 1)

    def test_stale_processing_batch_recovery_bounded_retries(self):
        analysis = {"_id": "analysis", "status": "processing", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        # Interrupted batch that has reached max retries (3)
        stale_time = service._now() - service.timedelta(seconds=500)
        batch = {"_id": "b1", "analysis_id": "analysis", "batch_index": 0, "status": "processing",
                 "attempt_count": 3, "last_activity_at": stale_time}
        batches = MemoryCollection([batch])

        service._recover_stale_batches("analysis", batches, lease_seconds=300, max_retries=3)

        # Should be marked failed, not pending
        self.assertEqual(batches.rows[0]["status"], "failed")
        self.assertIn("exceeded maximum retry attempts", batches.rows[0]["error_message"])

    def test_concurrent_worker_protection(self):
        analysis = {"_id": "analysis", "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        b0 = {"_id": "b0", "analysis_id": "analysis", "batch_index": 0, "status": "pending",
              "user_id": "user", "tender_id": "tender", "file_hash": "hash", "prompt_version": "5",
              "chunk_refs": [{"chunk_index": 1, "page_number": 1}]}
        b1 = {"_id": "b1", "analysis_id": "analysis", "batch_index": 1, "status": "pending",
              "user_id": "user", "tender_id": "tender", "file_hash": "hash", "prompt_version": "5",
              "chunk_refs": [{"chunk_index": 2, "page_number": 2}]}
        batches = MemoryCollection([b0, b1])
        analyses = MemoryCollection([analysis])
        chunks = MemoryCollection([
            {"chunk_index": 1, "page_number": 1, "text": "Passage 1", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
            {"chunk_index": 2, "page_number": 2, "text": "Passage 2", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
        ])
        pages = MemoryCollection([
            {"page_number": 1, "part_index": 0, "complete": True, "text": "Passage 1"},
            {"page_number": 2, "part_index": 0, "complete": True, "text": "Passage 2"},
        ])
        requirements = MemoryCollection()

        result = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"overview": {}, "requirements": []}'))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
        )
        fake_client = Mock()
        fake_client.models.generate_content.return_value = result

        with patch.object(service.genai, "Client", return_value=fake_client), patch.object(service, "_pace"):
            # Worker 1 runs
            w1 = service._process_batch(analysis, "token-w1", analyses, batches, requirements, pages, chunks)
            # Worker 2 runs
            w2 = service._process_batch(analysis, "token-w2", analyses, batches, requirements, pages, chunks)

        self.assertTrue(w1)
        self.assertTrue(w2)
        # Both distinct batches completed
        self.assertEqual(batches.rows[0]["status"], "completed")
        self.assertEqual(batches.rows[1]["status"], "completed")

    def test_pending_and_failed_batches_prevent_false_completion(self):
        analysis = {"_id": "analysis", "total_processing_ms": 0, "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        analyses = MemoryCollection([dict(analysis)])
        requirements = MemoryCollection()
        chunks = MemoryCollection([
            {"chunk_index": 1, "page_number": 1, "text": "Text 1", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
            {"chunk_index": 2, "page_number": 2, "text": "Text 2", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
        ])

        # Case A: Batch 0 completed, Batch 1 pending -> status must be 'processing', NOT 'completed'
        b0 = {"_id": "b0", "analysis_id": "analysis", "status": "completed", "chunk_refs": [{"chunk_index": 1, "page_number": 1}]}
        b1 = {"_id": "b1", "analysis_id": "analysis", "status": "pending", "chunk_refs": [{"chunk_index": 2, "page_number": 2}]}
        batches = MemoryCollection([b0, b1])
        status = service._refresh_counts(analysis, analyses, batches, requirements, chunks=chunks)
        self.assertEqual(status, "processing")
        self.assertEqual(analyses.rows[0]["status"], "processing")
        self.assertFalse(analyses.rows[0]["coverage"]["complete"])

        # Case B: Batch 0 completed, Batch 1 failed -> status must be 'failed', NOT 'completed'
        batches.rows[1]["status"] = "failed"
        status = service._refresh_counts(analysis, analyses, batches, requirements, chunks=chunks)
        self.assertEqual(status, "failed")
        self.assertEqual(analyses.rows[0]["status"], "failed")

    def test_live_progress_replaces_split_parent_with_logical_children(self):
        batches = MemoryCollection([
            {"_id": "parent", "analysis_id": "analysis", "status": "pending", "attempt_count": 0},
            {"_id": "other", "analysis_id": "analysis", "status": "pending", "attempt_count": 0},
        ])
        initial = service._batch_progress("analysis", batches)
        self.assertEqual(initial["total_batches"], 2)
        self.assertEqual(initial["completed_batches"], 0)

        batches.rows[0]["status"] = "split"
        batches.rows[1]["status"] = "completed"
        for index in range(4):
            batches.rows.append({
                "_id": f"child-{index}", "analysis_id": "analysis",
                "status": "completed", "attempt_count": 1,
            })
        live = service._batch_progress("analysis", batches)
        self.assertEqual(live["total_batches"], 5)
        self.assertEqual(live["completed_batches"], 5)
        self.assertEqual(live["workflow_completed_batches"], 5)
        self.assertEqual(live["split_batches"], 1)

    def test_all_pages_completed_and_saved_before_successful_completion(self):
        analysis = {"_id": "analysis", "total_processing_ms": 0, "user_id": "user", "tender_id": "tender", "file_hash": "hash"}
        analyses = MemoryCollection([dict(analysis)])
        chunks = MemoryCollection([
            {"chunk_index": 1, "page_number": 1, "text": "Text 1", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
            {"chunk_index": 2, "page_number": 2, "text": "Text 2", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
            {"chunk_index": 3, "page_number": 3, "text": "Text 3", "user_id": "user", "tender_id": "tender", "file_hash": "hash"},
        ])
        req = {"_id": "r1", "analysis_id": "analysis", "validation_status": "verified", "eligible_for_phase7": True}
        requirements = MemoryCollection([req])

        # Only pages 1 and 2 completed; page 3 has not completed
        b0 = {"_id": "b0", "analysis_id": "analysis", "status": "completed", "chunk_refs": [{"chunk_index": 1, "page_number": 1}]}
        b1 = {"_id": "b1", "analysis_id": "analysis", "status": "completed", "chunk_refs": [{"chunk_index": 2, "page_number": 2}]}
        batches = MemoryCollection([b0, b1])

        status = service._refresh_counts(analysis, analyses, batches, requirements, chunks=chunks)
        # Incomplete coverage prevents 'completed' status
        self.assertEqual(status, "needs_review")
        self.assertFalse(analyses.rows[0]["coverage"]["complete"])
        self.assertEqual(analyses.rows[0]["coverage"]["unexamined_pages"], 1)
        self.assertEqual(analyses.rows[0]["coverage"]["unexamined_page_numbers"], [3])

        # Now page 3 batch completes
        b2 = {"_id": "b2", "analysis_id": "analysis", "status": "completed", "chunk_refs": [{"chunk_index": 3, "page_number": 3}]}
        batches.rows.append(b2)

        status = service._refresh_counts(analysis, analyses, batches, requirements, chunks=chunks)
        # All pages complete and verified -> 'completed'
        self.assertEqual(status, "completed")
        self.assertTrue(analyses.rows[0]["coverage"]["complete"])
        self.assertEqual(analyses.rows[0]["coverage"]["unexamined_pages"], 0)

    def test_skipped_leaf_finishes_with_warnings_and_discloses_text_coverage(self):
        analysis = {"_id": "analysis", "total_processing_ms": 0, "user_id": "user",
                    "tender_id": "tender", "file_hash": "hash"}
        analyses = MemoryCollection([dict(analysis)])
        chunks = MemoryCollection([{"chunk_index": 1, "page_number": 1, "text": "0123456789",
                                    "user_id": "user", "tender_id": "tender", "file_hash": "hash"}])
        batches = MemoryCollection([
            {"_id": "parent", "analysis_id": "analysis", "status": "split",
             "chunk_refs": [{"chunk_index": 1, "page_number": 1}]},
            {"_id": "done-a", "analysis_id": "analysis", "status": "completed",
             "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 0, "text_end": 4}]},
            {"_id": "skipped", "analysis_id": "analysis", "status": "skipped",
             "skipped_reason": "Output limit", "skipped_text_ranges": [{"page_number": 1, "text_start": 4, "text_end": 6}],
             "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 4, "text_end": 6}]},
            {"_id": "done-b", "analysis_id": "analysis", "status": "completed",
             "chunk_refs": [{"chunk_index": 1, "page_number": 1, "text_start": 6, "text_end": 10}]},
        ])
        requirements = MemoryCollection()
        status = service._refresh_counts(analysis, analyses, batches, requirements, chunks=chunks)
        self.assertEqual(status, "completed_with_warnings")
        self.assertFalse(analyses.rows[0]["coverage"]["complete"])
        self.assertEqual(analyses.rows[0]["coverage"]["text_covered_chars"], 8)
        self.assertEqual(analyses.rows[0]["coverage"]["text_skipped_chars"], 2)
        public = service._public_analysis(analyses.rows[0], requirements, batches)
        self.assertEqual(public["progress"]["completed_batches"], 2)
        self.assertEqual(public["progress"]["skipped_batches"], 1)
        self.assertEqual(public["skipped_passages"][0]["reason"], "Output limit")
        analyses.rows[0]["provider_policy_version"] = 1
        with patch.object(service, "_process_batch") as process:
            resumed = service._advance(analyses.rows[0], analyses, batches, requirements,
                                       MemoryCollection(), chunks)
        process.assert_not_called()
        self.assertEqual(resumed["status"], "completed_with_warnings")
        self.assertEqual(resumed["progress"]["skipped_batches"], 1)


if __name__ == "__main__":
    unittest.main()
