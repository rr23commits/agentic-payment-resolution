"""Google Gemini generateContent adapter for the provider-neutral agent loop."""

import json
import logging
import os
import re
import socket
import time
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent.openrouter import openrouter_model
from agent.tools import TOOL_DEFINITIONS


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
LOGGER = logging.getLogger(__name__)
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.25
_GEMINI_QUOTA_EXHAUSTED = False
_GEMINI_TYPES = {"array": "ARRAY", "boolean": "BOOLEAN", "integer": "INTEGER", "number": "NUMBER", "null": "NULL", "object": "OBJECT", "string": "STRING"}
_SCHEMA_FIELDS = ("description", "format", "nullable", "enum", "minItems", "maxItems", "minProperties", "maxProperties", "minimum", "maximum", "pattern", "propertyOrdering")


class GeminiFallbackError(RuntimeError):
    """A Gemini provider failure for which the configured fallback may be used."""


class GeminiQuotaError(GeminiFallbackError):
    """Gemini daily quota is exhausted and should not be retried."""


def gemini_model(context: dict) -> dict:
    """Turn loop context into one Gemini function-call action."""
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be set")
    request = Request(
        f"{GEMINI_URL}/{quote(model, safe='')}:generateContent?key={quote(api_key, safe='')}",
        data=json.dumps(_request_body(context, model), default=_json_default).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            return _parse_response(payload, context.get("tools", []))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            LOGGER.error("Gemini request failed: HTTP %s response=%s", error.code, _redact(body))
            if error.code == 429 and _is_quota_exhausted(body):
                global _GEMINI_QUOTA_EXHAUSTED
                _GEMINI_QUOTA_EXHAUSTED = True
                raise GeminiQuotaError("Gemini daily quota exhausted") from error
            if error.code not in _TRANSIENT_STATUS_CODES or attempt == _MAX_ATTEMPTS - 1:
                if error.code in _TRANSIENT_STATUS_CODES:
                    raise GeminiFallbackError(f"Gemini request failed with HTTP {error.code}") from error
                raise RuntimeError(f"Gemini request failed with HTTP {error.code}") from error
            time.sleep(_retry_delay(error.headers, attempt))
        except (TimeoutError, socket.timeout) as error:
            LOGGER.error("Gemini request failed: timeout=%s", _redact(str(error)))
            if attempt == _MAX_ATTEMPTS - 1:
                raise GeminiFallbackError("Gemini request timed out") from error
            time.sleep(_retry_delay(None, attempt))
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                LOGGER.error("Gemini request failed: timeout=%s", _redact(str(error.reason)))
                if attempt == _MAX_ATTEMPTS - 1:
                    raise GeminiFallbackError("Gemini request timed out") from error
                time.sleep(_retry_delay(None, attempt))
                continue
            LOGGER.error("Gemini request failed: %s", _redact(str(error)))
            raise GeminiFallbackError("Gemini request failed") from error
    raise RuntimeError("Gemini request failed")


def gemini_first_model(context: dict) -> dict:
    """Use Gemini first, falling back only when its provider is unavailable."""
    if _GEMINI_QUOTA_EXHAUSTED:
        return openrouter_model(context)
    try:
        return gemini_model(context)
    except GeminiFallbackError:
        return openrouter_model(context)


def _is_quota_exhausted(body: str) -> bool:
    body = body.lower()
    return (
        "resource_exhausted" in body
        or "generaterequestsperdaypermodel-freetier" in body
        or "generaterequestsperdayperproject-freetier" in body
    )


def _redact(value: str) -> str:
    return re.sub(r"(?i)(api[_ -]?key|secret|token|password|authorization)\s*[:=]\s*[\"']?[^,\s\"'}]+", r"\1=<redacted>", value)


def _retry_delay(headers: object, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                return max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(parsedate_to_datetime(retry_after).tzinfo)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return _BACKOFF_SECONDS * (2 ** attempt)


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _request_body(context: dict, model: str) -> dict:
    tools = context.get("tools")
    if not isinstance(context.get("request"), str) or not isinstance(context.get("instructions"), str):
        raise ValueError("Model context requires request and instructions")
    if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
        raise ValueError("Model context requires tools")
    return {
        "systemInstruction": {"parts": [{"text": context["instructions"]}]},
        "contents": _contents(context),
        "tools": [{"functionDeclarations": [_function_declaration(tool) for tool in tools]}],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
    }


def _function_declaration(tool: dict) -> dict:
    parameters = tool.get("parameters") or {"type": "object", "properties": {}}
    return {
        "name": tool.get("name"),
        "description": tool.get("description", ""),
        "parameters": _gemini_schema(parameters),
    }


def _gemini_schema(schema: dict) -> dict:
    if not isinstance(schema, dict):
        return {"type": "OBJECT", "properties": {}}
    result = {}
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        result["type"] = _GEMINI_TYPES.get(schema_type.lower(), schema_type.upper())
    elif "properties" in schema:
        result["type"] = "OBJECT"
    for field in _SCHEMA_FIELDS:
        if field in schema:
            result[field] = schema[field]
    if result.get("type") == "OBJECT":
        result["properties"] = {name: _gemini_schema(value) for name, value in schema.get("properties", {}).items()}
        required = schema.get("required")
        if isinstance(required, list) and required:
            result["required"] = required
    elif result.get("type") == "ARRAY" and isinstance(schema.get("items"), dict):
        result["items"] = _gemini_schema(schema["items"])
    return result or {"type": "OBJECT", "properties": {}}


def _contents(context: dict) -> list[dict]:
    contents = [{"role": "user", "parts": [{"text": context["request"]}]}]
    for entry in context.get("history", []):
        assistant = entry.get("_assistant_message")
        if not isinstance(assistant, dict):
            continue
        contents.append(assistant)
        call = _function_call(assistant)
        if call:
            response = entry.get("result", {"error": entry.get("error", "Tool failed")})
            response = response if isinstance(response, dict) else {"result": response}
            function_response = {"name": call["name"], "response": response}
            if isinstance(call.get("id"), str):
                function_response["id"] = call["id"]
            contents.append({"role": "user", "parts": [{"functionResponse": function_response}]})
    return contents


def _function_call(content: dict) -> dict | None:
    for part in content.get("parts", []):
        if not isinstance(part, dict):
            continue
        call = part.get("function_call", part.get("functionCall"))
        if isinstance(call, dict):
            return call
    return None


def _parse_response(payload: object, tools: list[dict]) -> dict:
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Gemini response has no candidate content") from None
    if not isinstance(parts, list):
        raise ValueError("Gemini response parts are invalid")
    calls = []
    for part in parts:
        if isinstance(part, dict):
            call = part.get("function_call", part.get("functionCall"))
            if isinstance(call, dict):
                calls.append(call)
    if len(calls) != 1:
        raise ValueError("Gemini response must contain exactly one function call")
    call = calls[0]
    name, arguments = call.get("name"), call.get("args", {})
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        raise ValueError("Gemini function call is invalid")
    if name not in {tool.get("name") for tool in tools}:
        raise ValueError("Gemini function call is unavailable")
    result = {"tool": name, "arguments": arguments, "_assistant_message": payload["candidates"][0]["content"]}
    if isinstance(call.get("id"), str):
        result["_tool_call_id"] = call["id"]
    return result
