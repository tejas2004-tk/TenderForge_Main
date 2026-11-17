import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import _effective_gemini_model


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


if __name__ == "__main__":
    unittest.main()
