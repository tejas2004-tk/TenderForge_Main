import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import _effective_gemini_model, _provider_order


class GeminiModelConfigurationTests(unittest.TestCase):
    def test_local_development_dotenv_model_overrides_inherited_model(self):
        self.assertEqual(
            _effective_gemini_model(
                "gemini-from-dotenv",
                {"GEMINI_MODEL": "stale-inherited-model"},
                local_development=True,
            ),
            "gemini-from-dotenv",
        )

    def test_deployment_environment_model_keeps_environment_precedence(self):
        self.assertEqual(
            _effective_gemini_model(
                "gemini-from-dotenv",
                {"GEMINI_MODEL": "deployment-model"},
                local_development=False,
            ),
            "deployment-model",
        )

    def test_dotenv_model_is_used_when_environment_has_no_model(self):
        self.assertEqual(_effective_gemini_model("gemini-from-dotenv", {}, False), "gemini-from-dotenv")

    def test_provider_order_defaults_to_approved_priority(self):
        self.assertEqual(_provider_order(None), ["groq", "openrouter", "gemini"])

    def test_provider_order_rejects_duplicates_and_unknown_names(self):
        for value in ("groq,groq", "openai,gemini", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _provider_order(value)


if __name__ == "__main__":
    unittest.main()
