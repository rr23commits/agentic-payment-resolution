import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from agent.openrouter import _parse_response, _request_body, openrouter_model
from agent.tools import CREATE_CART_PARAMETERS, TOOL_DEFINITIONS


TOOLS = [{"name": "search_catalogue", "description": "Find products."}]

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "search_catalogue", "arguments": '{"query":"book"}'},
}

VALID_RESPONSE_FIXTURES = [
    {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
    {"role": "assistant", "content": [{"type": "text", "text": "Searching."}], "tool_calls": [TOOL_CALL]},
    {"role": "assistant", "content": [], "reasoning": "I should search.", "tool_calls": [TOOL_CALL]},
    {"role": "assistant", "content": None, "reasoning_details": [{"type": "reasoning.text", "text": "Search first."}], "tool_calls": [TOOL_CALL]},
    {"role": "assistant", "content": None, "reasoning_content": "Search first.", "tool_calls": [TOOL_CALL]},
]


class OpenRouterAdapterTests(unittest.TestCase):
    @patch("agent.openrouter.urlopen")
    def test_uses_free_model_default(self, urlopen) -> None:
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda *args: None})()
        response.read = lambda: b'{"choices":[{"message":{"tool_calls":[{"id":"call_1","function":{"name":"search_catalogue","arguments":"{}"}}]}}]}'
        urlopen.return_value = response
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "key"}, clear=True):
            openrouter_model({"request": "Find", "instructions": "Use tools.", "tools": TOOLS})
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["model"], "openai/gpt-oss-20b:free")

    @patch("agent.openrouter.urlopen")
    def test_http_error_keeps_status_and_provider_message(self, urlopen) -> None:
        urlopen.side_effect = HTTPError(
            "https://openrouter.ai", 429, "Too Many Requests", {},
            BytesIO(b'{"error":{"message":"rate limit exceeded"}}'),
        )
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "key", "OPENROUTER_MODEL": "model"}):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429: rate limit exceeded"):
                openrouter_model({"request": "Find a book", "instructions": "Use tools.", "tools": TOOLS})

    def test_parses_an_allowed_tool_call(self) -> None:
        message = VALID_RESPONSE_FIXTURES[0]
        payload = {"choices": [{"message": message}]}
        result = _parse_response(payload, TOOLS)
        self.assertEqual(result["tool"], "search_catalogue")
        self.assertEqual(result["arguments"], {"query": "book"})
        self.assertEqual(result["_assistant_message"], message)
        self.assertEqual(result["_tool_call_id"], "call_1")

    def test_accepts_content_arrays_and_reasoning_fields_with_one_tool_call(self) -> None:
        for message in VALID_RESPONSE_FIXTURES:
            with self.subTest(message_keys=sorted(message)):
                result = _parse_response({"choices": [{"message": message}]}, TOOLS)
                self.assertEqual(result["tool"], "search_catalogue")
                self.assertIs(result["_assistant_message"], message)

    def test_rejects_invalid_model_output(self) -> None:
        payload = {"choices": [{"message": {"tool_calls": [{"function": {"name": "checkout", "arguments": "{}"}}]}}]}
        with self.assertRaises(ValueError):
            _parse_response(payload, TOOLS)

    def test_requires_a_tool_call(self) -> None:
        context = {"request": "Find a book", "instructions": "Use only listed tools.", "tools": TOOLS, "history": []}
        with self.assertRaisesRegex(ValueError, "one tool call"):
            _parse_response({"choices": [{"message": {"content": "Here are the options."}}]}, TOOLS)
        body = _request_body(context, "~openai/gpt-latest")
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(body["tools"][0]["function"]["name"], "search_catalogue")

    def test_rejects_multiple_tool_calls(self) -> None:
        payload = {"choices": [{"message": {"tool_calls": [
            {"id": "call_1", "function": {"name": "search_catalogue", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "search_catalogue", "arguments": "{}"}},
        ]}}]}
        with self.assertRaisesRegex(ValueError, "one tool call"):
            _parse_response(payload, TOOLS)

    def test_create_cart_schema_is_nested_and_constrained(self) -> None:
        tool = {"name": "create_cart", "description": "Create a cart.", "parameters": CREATE_CART_PARAMETERS}
        schema = _request_body({"request": "Buy", "instructions": "Use tools.", "tools": [tool]}, "model")["tools"][0]["function"]["parameters"]
        item = schema["properties"]["items"]["items"]
        self.assertEqual(schema["required"], ["items"])
        self.assertEqual(item["required"], ["product_id", "quantity"])
        self.assertEqual(item["properties"]["quantity"], {"type": "integer", "minimum": 1})

    def test_every_tool_has_a_complete_object_schema(self) -> None:
        for tool in TOOL_DEFINITIONS:
            parameters = tool["parameters"]
            self.assertEqual(parameters["type"], "object")
            self.assertIn("properties", parameters)
            self.assertIn("required", parameters)

    def test_tool_result_messages_follow_assistant_call(self) -> None:
        assistant = {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_catalogue", "arguments": '{"query":"book"}'}}]}
        body = _request_body({
            "request": "Find a book", "instructions": "Use tools.", "tools": TOOLS,
            "history": [{"tool": "search_catalogue", "tool_call_id": "call_1", "_assistant_message": assistant, "result": [{"id": "book_1"}]}],
        }, "model")
        self.assertEqual(body["messages"][2], assistant)
        self.assertEqual(body["messages"][3], {"role": "tool", "tool_call_id": "call_1", "name": "search_catalogue", "content": '[{"id": "book_1"}]'})
