import unittest
from unittest.mock import patch

from agent.loop import run_agent


class AgentProviderErrorTests(unittest.TestCase):
    @patch("agent.loop.tools_for")
    def test_provider_error_returns_safe_result(self, tools_for) -> None:
        with patch("agent.loop.TOOL_DEFINITIONS", []):
            with self.assertLogs("agent.loop", level="ERROR") as logs:
                result = run_agent("Buy a book", customer_id="customer_demo", model=RuntimeErrorModel())
        self.assertEqual(result["message"], "I could not complete this request safely.")
        self.assertEqual(result["history"], [{"error": "Model request failed"}])
        self.assertIn("provider unavailable", logs.output[0])

    @patch("agent.loop.tools_for")
    def test_provider_log_redacts_secret_like_values(self, tools_for) -> None:
        with patch("agent.loop.TOOL_DEFINITIONS", []):
            with self.assertLogs("agent.loop", level="ERROR") as logs:
                run_agent("Buy a book", customer_id="customer_demo", model=SecretErrorModel())
        self.assertNotIn("sk-secret", logs.output[0])
        self.assertIn("api_key=<redacted>", logs.output[0])


class RuntimeErrorModel:
    def __call__(self, _context: dict) -> dict:
        raise RuntimeError("provider unavailable")


class SecretErrorModel:
    def __call__(self, _context: dict) -> dict:
        raise RuntimeError("HTTP 401: api_key=sk-secret")
