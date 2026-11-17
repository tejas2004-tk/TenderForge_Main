import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import provider_service as providers


class ProviderAdapterTests(unittest.TestCase):
    def _response(self, status, payload, headers=None):
        return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: payload)

    @patch("services.provider_service.httpx.Client")
    def test_openrouter_normalizes_content_finish_reason_and_usage(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(200, {
            "id": "req-1", "choices": [{"finish_reason": "length", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }, {"x-request-id": "req-1"})
        result = providers.request_openrouter(
            [{"role": "user", "content": "prompt"}], model="openai/gpt-oss-120b",
            api_key="secret", base_url="https://openrouter.ai/api/v1", timeout_seconds=10,
            connect_timeout_seconds=3, max_output_tokens=100,
        )
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.total_tokens, 7)
        sent = factory.return_value.__enter__.return_value.post.call_args
        self.assertEqual(sent.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertIn("max_tokens", sent.kwargs["json"])
        self.assertNotIn("include_reasoning", sent.kwargs["json"])
        self.assertNotIn("secret", str(result))

    @patch("services.provider_service.httpx.Client")
    def test_groq_gpt_oss_uses_reasoning_off_and_completion_token_budget(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(200, {
            "id": "req-groq", "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 18, "total_tokens": 30},
        })
        result = providers.request_groq(
            [{"role": "user", "content": "prompt"}], model="openai/gpt-oss-120b",
            api_key="secret", timeout_seconds=10, connect_timeout_seconds=3, max_output_tokens=2345,
        )
        body = factory.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        self.assertEqual(body["max_completion_tokens"], 2345)
        self.assertNotIn("max_tokens", body)
        self.assertIs(body["include_reasoning"], False)
        self.assertEqual(result.content, "{}")
        self.assertEqual(result.request_id, "req-groq")

    @patch("services.provider_service.httpx.Client")
    def test_groq_null_content_preserves_safe_metadata_without_reasoning_text(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(200, {
            "id": "req-empty", "choices": [{"finish_reason": "stop", "message": {
                "content": None, "reasoning": "must never be forwarded",
            }}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
        }, {"x-request-id": "req-header"})
        with self.assertRaises(providers.ProviderRequestError) as caught:
            providers.request_groq(
                [{"role": "user", "content": "prompt"}], model="openai/gpt-oss-120b",
                api_key="secret", timeout_seconds=10, connect_timeout_seconds=3, max_output_tokens=100,
            )
        error = caught.exception
        self.assertEqual(error.status_code, 200)
        self.assertEqual(error.category, "reasoning_only_response")
        self.assertEqual(error.finish_reason, "stop")
        self.assertEqual((error.input_tokens, error.output_tokens, error.total_tokens), (5, 9, 14))
        self.assertEqual(error.request_id, "req-header")
        self.assertTrue(error.reasoning_present)
        self.assertNotIn("must never be forwarded", str(error))

    @patch("services.provider_service.httpx.Client")
    def test_groq_empty_truncated_output_preserves_finish_and_usage(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(200, {
            "id": "req-truncated", "choices": [{"finish_reason": "length", "message": {
                "content": None, "reasoning": "private reasoning",
            }}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 100, "total_tokens": 107},
        })
        result = providers.request_groq(
            [{"role": "user", "content": "prompt"}], model="openai/gpt-oss-120b",
            api_key="secret", timeout_seconds=10, connect_timeout_seconds=3, max_output_tokens=100,
        )
        self.assertEqual(result.content, "")
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (7, 100, 107))
        self.assertTrue(result.reasoning_present)

    @patch("services.provider_service.httpx.Client")
    def test_reasoning_only_response_is_not_analysis_content(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(200, {
            "choices": [{"finish_reason": "stop", "message": {"content": None, "reasoning": "private reasoning"}}],
        })
        with self.assertRaises(providers.ProviderRequestError) as caught:
            providers.request_openrouter(
                [{"role": "user", "content": "prompt"}], model="m", api_key="secret",
                base_url="https://openrouter.ai/api/v1", timeout_seconds=10,
                connect_timeout_seconds=3, max_output_tokens=100,
            )
        self.assertEqual(caught.exception.category, "reasoning_only_response")

    @patch("services.provider_service.httpx.Client")
    def test_auth_error_is_classified_and_secret_redacted(self, factory):
        factory.return_value.__enter__.return_value.post.return_value = self._response(
            401, {"error": {"message": "Bearer secret rejected"}}, {"Retry-After": "4"},
        )
        with self.assertRaises(providers.ProviderRequestError) as caught:
            providers.request_openrouter(
                [{"role": "user", "content": "prompt"}], model="m", api_key="secret",
                base_url="https://openrouter.ai/api/v1", timeout_seconds=10,
                connect_timeout_seconds=3, max_output_tokens=100,
            )
        self.assertEqual(caught.exception.category, "authentication")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.retry_after_seconds, 4)


if __name__ == "__main__":
    unittest.main()
