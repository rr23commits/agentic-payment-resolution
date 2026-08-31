import os
import json
import unittest
import agent.gemini as gemini_adapter
from datetime import datetime
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

from agent.gemini import _parse_response, _request_body, gemini_first_model, gemini_model
from agent.loop import run_agent
from agent.tools import TOOL_DEFINITIONS


TOOLS = [{"name": "search_catalogue", "description": "Find products.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
CALL = {"name": "search_catalogue", "args": {"query": "book"}}


class GeminiAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        gemini_adapter._GEMINI_QUOTA_EXHAUSTED = False

    def _response(self, name="search_catalogue", args=None, call_id="call-1", signature="sig-1"):
        args = {} if args is None else args
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda *args: None})()
        response.read = lambda: json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args, "id": call_id}, "thoughtSignature": signature}]}}]}).encode()
        return response

    def test_converts_tool_definitions_to_function_declarations(self) -> None:
        body = _request_body({"request": "Find a book", "instructions": "Use tools.", "tools": TOOLS}, "gemini-2.5-flash")
        declaration = body["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "search_catalogue")
        self.assertEqual(declaration["description"], "Find products.")
        self.assertEqual(declaration["parameters"], {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]})
        self.assertEqual(body["toolConfig"]["functionCallingConfig"]["mode"], "ANY")

    def test_converts_parameterless_tools_to_empty_object_schemas(self) -> None:
        tools = [tool for tool in TOOL_DEFINITIONS if tool["name"] in {"get_mandate", "respond_to_customer"}]
        declarations = _request_body({"request": "Check", "instructions": "Use tools.", "tools": tools}, "gemini-2.5-flash")["tools"][0]["functionDeclarations"]
        mandate = next(item for item in declarations if item["name"] == "get_mandate")
        respond = next(item for item in declarations if item["name"] == "respond_to_customer")
        self.assertEqual(mandate["parameters"], {"type": "OBJECT", "properties": {}})
        self.assertEqual(respond["parameters"], {"type": "OBJECT", "properties": {"message": {"type": "STRING"}}, "required": ["message"]})

    def test_parses_a_gemini_function_call(self) -> None:
        content = {"role": "model", "parts": [{"text": "Searching."}, {"functionCall": CALL, "thoughtSignature": "sig-1"}]}
        result = _parse_response({"candidates": [{"content": content}]}, TOOLS)
        self.assertEqual(result["tool"], "search_catalogue")
        self.assertEqual(result["arguments"], {"query": "book"})
        self.assertEqual(result["_assistant_message"], content)

    def test_parses_respond_to_customer(self) -> None:
        tools = [next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "respond_to_customer")]
        payload = {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "respond_to_customer", "args": {"message": "Done."}}}]}}]}
        result = _parse_response(payload, tools)
        self.assertEqual((result["tool"], result["arguments"]), ("respond_to_customer", {"message": "Done."}))

    def test_rejects_missing_and_multiple_function_calls(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _parse_response({"candidates": [{"content": {"parts": [{"text": "No call."}]}}]}, TOOLS)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _parse_response({"candidates": [{"content": {"parts": [{"functionCall": CALL}, {"functionCall": CALL}]}}]}, TOOLS)

    def test_sends_tool_result_back_as_function_response(self) -> None:
        assistant = {"role": "model", "parts": [{"functionCall": {**CALL, "id": "call-1", "thoughtSignature": "sig-1"}}]}
        body = _request_body({
            "request": "Find a book", "instructions": "Use tools.", "tools": TOOLS,
            "history": [{"tool": "search_catalogue", "_assistant_message": assistant, "result": [{"id": "book_1"}]}],
        }, "gemini-2.5-flash")
        self.assertEqual(body["contents"][1], assistant)
        self.assertEqual(body["contents"][2], {"role": "user", "parts": [{"functionResponse": {
            "name": "search_catalogue", "response": {"result": [{"id": "book_1"}]}, "id": "call-1",
        }}]})

    @patch("agent.gemini.urlopen")
    def test_serializes_datetime_in_tool_result(self, urlopen) -> None:
        urlopen.return_value = self._response()
        assistant = {"role": "model", "parts": [{"function_call": CALL}]}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            gemini_model({
                "request": "Find a book", "instructions": "Use tools.", "tools": TOOLS,
                "history": [{"_assistant_message": assistant, "result": {"expires_at": datetime(2026, 8, 30, 12, 0)}}],
            })
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["contents"][2]["parts"][0]["functionResponse"]["response"]["expires_at"], "2026-08-30T12:00:00")

    @patch("agent.gemini.urlopen")
    def test_uses_environment_model_and_key(self, urlopen) -> None:
        urlopen.return_value = self._response()
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key", "GEMINI_MODEL": "gemini-test"}):
            result = gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        self.assertIn("models/gemini-test:generateContent", urlopen.call_args.args[0].full_url)
        self.assertNotIn("key=", urlopen.call_args.args[0].full_url)
        self.assertEqual(urlopen.call_args.args[0].headers["X-goog-api-key"], "key")

    @patch("agent.gemini.openrouter_model")
    @patch("agent.gemini.urlopen")
    def test_gemini_success_does_not_fallback(self, urlopen, openrouter) -> None:
        urlopen.return_value = self._response()
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = gemini_first_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        openrouter.assert_not_called()

    @patch("agent.gemini.openrouter_model", return_value={"tool": "search_catalogue", "arguments": {"query": "book"}})
    def test_missing_gemini_key_falls_back(self, openrouter) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}, clear=True):
            result = gemini_first_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        openrouter.assert_called_once()

    @patch("agent.gemini.openrouter_model", return_value={"tool": "search_catalogue", "arguments": {"query": "book"}})
    @patch("agent.gemini.urlopen")
    def test_daily_quota_falls_back_without_retry(self, urlopen, openrouter) -> None:
        error = HTTPError("url", 429, "quota", {}, BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED","message":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}'))
        urlopen.side_effect = error
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = gemini_first_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        self.assertEqual(urlopen.call_count, 1)
        openrouter.assert_called_once()

    @patch("agent.gemini.openrouter_model", return_value={"tool": "search_catalogue", "arguments": {"query": "book"}})
    @patch("agent.gemini.urlopen")
    def test_subsequent_requests_skip_exhausted_gemini(self, urlopen, openrouter) -> None:
        error = HTTPError("url", 429, "quota", {}, BytesIO(b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'))
        urlopen.side_effect = error
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            gemini_first_model({"request": "First", "instructions": "Use tools.", "tools": TOOLS})
            gemini_first_model({"request": "Second", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(openrouter.call_count, 2)

    @patch("agent.gemini.openrouter_model", return_value={"tool": "search_catalogue", "arguments": {"query": "book"}})
    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_transient_failure_retries_then_falls_back(self, urlopen, sleep, openrouter) -> None:
        error = HTTPError("url", 503, "unavailable", {}, BytesIO(b'{"error":{"message":"busy"}}'))
        urlopen.side_effect = [error, error, error]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = gemini_first_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        openrouter.assert_called_once()

    @patch("agent.gemini.openrouter_model")
    @patch("agent.gemini.urlopen")
    def test_both_providers_failing_reaches_existing_safe_loop_response(self, urlopen, openrouter) -> None:
        urlopen.side_effect = HTTPError("url", 429, "quota", {}, BytesIO(b'{"error":{"message":"GenerateRequestsPerDayPerProject-FreeTier"}}'))
        openrouter.side_effect = RuntimeError("OpenRouter unavailable")
        with patch("agent.loop.tools_for"), patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = run_agent("Find", customer_id="customer_demo", model=gemini_first_model)
        self.assertEqual(result["message"], "I could not complete this request safely.")

    @patch("agent.gemini.openrouter_model", return_value={"tool": "respond_to_customer", "arguments": {"message": "Done."}})
    @patch("agent.gemini.urlopen")
    def test_fallback_does_not_duplicate_tool_execution(self, urlopen, openrouter) -> None:
        quota = HTTPError("url", 429, "quota", {}, BytesIO(b'{"error":{"message":"GenerateRequestsPerDayPerProject-FreeTier"}}'))
        urlopen.side_effect = [self._response("search_catalogue", {"query": "book"}), quota]
        calls = []
        fake_tools = type("Tools", (), {"search_catalogue": lambda self, **kwargs: calls.append(kwargs) or [{"id": "book"}], "respond_to_customer": lambda self, **kwargs: kwargs})()
        with patch("agent.loop.tools_for", return_value=fake_tools), patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = run_agent("Buy a book", customer_id="customer_demo", model=gemini_first_model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["message"], "Done.")
        openrouter.assert_called_once()

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_retries_transient_429_and_respects_retry_after(self, urlopen, sleep) -> None:
        error = HTTPError("url", 429, "busy", {"Retry-After": "3"}, BytesIO(b'{"error":{"message":"rate limited"}}'))
        urlopen.side_effect = [error, self._response()]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(result["tool"], "search_catalogue")
        sleep.assert_called_once_with(3.0)

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_retries_transient_503(self, urlopen, sleep) -> None:
        error = HTTPError("url", 503, "unavailable", {}, BytesIO(b'{"error":{"message":"busy"}}'))
        urlopen.side_effect = [error, self._response()]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_retries_timeout(self, urlopen, sleep) -> None:
        urlopen.side_effect = [TimeoutError("timed out"), self._response()]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_does_not_retry_400(self, urlopen, sleep) -> None:
        urlopen.side_effect = HTTPError("url", 400, "bad request", {}, BytesIO(b'{"error":{"message":"invalid"}}'))
        with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
                gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_retry_preserves_thought_signature_and_call_id(self, urlopen, sleep) -> None:
        error = HTTPError("url", 503, "unavailable", {}, BytesIO(b'{"error":{"message":"busy"}}'))
        urlopen.side_effect = [error, self._response()]
        assistant = {"role": "model", "parts": [{"functionCall": {**CALL, "id": "call-1"}, "thoughtSignature": "sig-1"}]}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            gemini_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS, "history": [{"_assistant_message": assistant, "result": {"ok": True}}]})
        body = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(body["contents"][1], assistant)
        self.assertEqual(body["contents"][2]["parts"][0]["functionResponse"]["id"], "call-1")

    @patch("agent.gemini.time.sleep")
    @patch("agent.gemini.urlopen")
    def test_retry_does_not_duplicate_agent_tool_execution(self, urlopen, sleep) -> None:
        error = HTTPError("url", 503, "unavailable", {}, BytesIO(b'{"error":{"message":"busy"}}'))
        urlopen.side_effect = [error, self._response("search_catalogue", {"query": "book"}), self._response("respond_to_customer", {"message": "Done."})]
        calls = []
        fake_tools = type("Tools", (), {"search_catalogue": lambda self, **kwargs: calls.append(kwargs) or [{"id": "book"}], "respond_to_customer": lambda self, **kwargs: kwargs})()
        with patch("agent.loop.tools_for", return_value=fake_tools), patch.dict(os.environ, {"GEMINI_API_KEY": "key"}):
            result = run_agent("Buy a book", customer_id="customer_demo", model=gemini_model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["message"], "Done.")
