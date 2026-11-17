import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from routes.test_routes import test_bp


class GeminiTestRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(GEMINI_API_KEY="secret-must-not-be-returned", GEMINI_MODEL="gemini-effective-model")
        self.app.register_blueprint(test_bp, url_prefix="/api")
        self.client = self.app.test_client()

    @patch("routes.test_routes.test_gemini_connection", return_value="connected")
    def test_response_reports_effective_model_without_api_key(self, test_connection):
        response = self.client.get("/api/test-gemini")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["model"], "gemini-effective-model")
        self.assertEqual(response.json["message"], "connected")
        self.assertNotIn("secret-must-not-be-returned", response.get_data(as_text=True))
        test_connection.assert_called_once_with(
            "secret-must-not-be-returned", "gemini-effective-model", self.app.config,
        )


if __name__ == "__main__":
    unittest.main()
