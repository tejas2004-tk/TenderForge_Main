import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import gemini_service


class FakeAPIError(Exception):
    def __init__(self, code, message="provider error", details=None, headers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.response = SimpleNamespace(headers=headers or {})


class GeminiServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "GEMINI_CONNECTION_TIMEOUT_MS": 12000,
            "GEMINI_CONNECTION_RETRY_ATTEMPTS": 3,
            "GEMINI_CONNECTION_RETRY_BASE_SECONDS": 1,
            "GEMINI_CONNECTION_RETRY_MAX_SECONDS": 5,
        }

    @patch("services.gemini_service.genai_errors.APIError", FakeAPIError)
    @patch("services.gemini_service.time.sleep")
    @patch("services.gemini_service.random.uniform", return_value=0.25)
    @patch("services.gemini_service.genai.Client")
    def test_retries_temporary_503_then_succeeds(self, client_factory, _jitter, sleep):
        client = client_factory.return_value
        client.models.generate_content.side_effect = [
            FakeAPIError(503), SimpleNamespace(text="connected"),
        ]

        result = gemini_service.test_gemini_connection("secret-key", "gemini-3.8-flash", self.settings)

        self.assertEqual(result, "connected")
        self.assertEqual(client.models.generate_content.call_count, 2)
        sleep.assert_called_once_with(0.25)
        options = client_factory.call_args.kwargs["http_options"]
        self.assertEqual(options["timeout"], 12000)
        self.assertEqual(options["retry_options"], {"attempts": 1})

    @patch("services.gemini_service.genai_errors.APIError", FakeAPIError)
    @patch("services.gemini_service.time.sleep")
    @patch("services.gemini_service.genai.Client")
    def test_retry_after_is_used_and_daily_quota_is_not_retried(self, client_factory, sleep):
        client = client_factory.return_value
        client.models.generate_content.side_effect = [
            FakeAPIError(503, headers={"Retry-After": "2.5"}),
            FakeAPIError(
                429, "Requests per day limit exceeded",
                {"error": {"message": "Requests per day limit exceeded"}},
            ),
        ]
        with self.assertRaises(gemini_service.GeminiServiceError) as caught:
            gemini_service.test_gemini_connection("secret-key", "gemini-3.8-flash", self.settings)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("daily quota", str(caught.exception))
        sleep.assert_called_once_with(2.5)
        self.assertEqual(client.models.generate_content.call_count, 2)

    @patch("services.gemini_service.genai.Client")
    def test_empty_response_is_not_success(self, client_factory):
        client_factory.return_value.models.generate_content.return_value = SimpleNamespace(text=" ")
        with self.assertRaises(gemini_service.GeminiServiceError) as caught:
            gemini_service.test_gemini_connection("secret-key", "gemini-3.8-flash", self.settings)
        self.assertEqual(caught.exception.status_code, 502)

    @patch("services.gemini_service.genai_errors.APIError", FakeAPIError)
    @patch("services.gemini_service.genai.Client")
    def test_authentication_and_missing_model_statuses_are_preserved(self, client_factory):
        for provider_status in (401, 403, 404):
            with self.subTest(status=provider_status):
                client = client_factory.return_value
                client.models.generate_content.side_effect = FakeAPIError(provider_status)
                with self.assertRaises(gemini_service.GeminiServiceError) as caught:
                    gemini_service.test_gemini_connection("secret-key", "gemini-3.8-flash", self.settings)
                self.assertEqual(caught.exception.status_code, provider_status)


if __name__ == "__main__":
    unittest.main()
