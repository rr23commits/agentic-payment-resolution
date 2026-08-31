"""OpenRouter adapter for the provider-neutral Phase 8 model callable."""

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LOGGER = logging.getLogger(__name__)


def openrouter_model(context: dict) -> dict:
    """Turn loop context into one OpenRouter response action."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-20b:free")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY must be set")
    request = Request(
        OPENROUTER_URL,
        data=json.dumps(_request_body(context, model)).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        try:
            details = json.loads(error.read(4096) or b"{}")
            details = details.get("error", {}).get("message", "") if isinstance(details, dict) else ""
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            details = ""
        suffix = f": {details}" if isinstance(details, str) and details else ""
        raise RuntimeError(f"OpenRouter request failed with HTTP {error.code}{suffix}") from error
    except URLError as error:
        raise RuntimeError("OpenRouter request failed") from error
    return _parse_response(payload, context.get("tools", []), _has_successful_checkout(context.get("history", [])))


def _request_body(context: dict, model: str) -> dict:
    """Use only the loop-provided instructions, tools, request, and history."""
    if not isinstance(context.get("request"), str) or not isinstance(context.get("instructions"), str):
        raise ValueError("Model context requires request and instructions")
    tools = context.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
        raise ValueError("Model context requires tools")
    return {
        "model": model,
        "messages": _messages(context),
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}),
                },
            }
            for tool in tools
        ],
        "tool_choice": "auto" if _has_successful_checkout(context.get("history", [])) else "required",
        "parallel_tool_calls": False,
    }


def _messages(context: dict) -> list[dict]:
    messages = [
        {"role": "system", "content": context["instructions"]},
        {"role": "user", "content": context["request"]},
    ]
    for entry in context.get("history", []):
        assistant = entry.get("_assistant_message")
        if isinstance(assistant, dict):
            messages.append(assistant)
        if entry.get("tool_call_id"):
            result = entry.get("result", {"error": entry.get("error", "Tool failed")})
            messages.append({
                "role": "tool",
                "tool_call_id": entry["tool_call_id"],
                "name": entry.get("tool", ""),
                "content": json.dumps(result, default=str),
            })
    return messages


def _parse_response(payload: object, tools: list[dict], allow_final: bool = False) -> dict:
    """Accept exactly one allowed tool call, preserving provider metadata untouched."""
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise _rejected(payload, "OpenRouter response has no assistant message") from None
    if not isinstance(message, dict):
        raise _rejected(payload, "OpenRouter assistant message is invalid")
    # Content and reasoning are non-executable metadata; only one tool call may drive this turn.
    calls = message.get("tool_calls")
    if allow_final and calls is None and isinstance(message.get("content"), str) and message["content"].strip():
        return {"final": message["content"]}
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise _rejected(payload, "OpenRouter response must contain one tool call")
    call = calls[0]
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not isinstance(call.get("id"), str) or not call["id"]:
        raise _rejected(payload, "OpenRouter tool call is invalid")
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (TypeError, json.JSONDecodeError):
        raise _rejected(payload, "OpenRouter tool arguments must be JSON") from None
    if function["name"] not in {tool.get("name") for tool in tools} or not isinstance(arguments, dict):
        raise _rejected(payload, "OpenRouter tool call is unavailable or invalid")
    return {
        "tool": function["name"], "arguments": arguments,
        "_assistant_message": message, "_tool_call_id": call["id"],
    }


def _has_successful_checkout(history: list[dict]) -> bool:
    return bool(
        history
        and history[-1].get("tool") == "start_checkout"
        and isinstance(history[-1].get("result"), dict)
        and history[-1]["result"].get("allowed") is True
    )


def _rejected(payload: object, reason: str) -> ValueError:
    LOGGER.warning(
        "Rejected OpenRouter assistant response: %s; parsed=%s",
        reason,
        json.dumps(_safe_response_summary(payload), separators=(",", ":")),
    )
    return ValueError(reason)


def _safe_response_summary(payload: object) -> dict:
    """Log response shape only; omit content, arguments, IDs, and provider data."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {"top_level_keys": sorted(payload)}
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return {"top_level_keys": sorted(payload), "message_type": type(message).__name__}
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        tool_names = []
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            tool_names.append(function.get("name") if isinstance(function, dict) else None)
        calls_summary = {"count": len(calls), "tool_names": tool_names}
    else:
        calls_summary = {"type": type(calls).__name__}
    return {
        "top_level_keys": sorted(payload),
        "message_keys": sorted(message),
        "tool_calls": calls_summary,
        "has_content": isinstance(message.get("content"), str),
    }
