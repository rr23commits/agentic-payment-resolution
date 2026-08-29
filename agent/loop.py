"""Model-selected execution over the customer-scoped tool allowlist."""

import logging
import re
from collections.abc import Callable

from agent.tools import TOOL_DEFINITIONS, tools_for


LOGGER = logging.getLogger(__name__)


def run_agent(
    request: str,
    *,
    customer_id: str,
    model: Callable[[dict], dict],
    max_steps: int = 12,
) -> dict:
    """Let a model choose one tool per turn until it responds to the customer."""
    if not request or max_steps < 1:
        raise ValueError("request and a positive max_steps are required")
    tools = tools_for(customer_id)
    history: list[dict] = []
    for _ in range(max_steps):
        try:
            action = model(
                {
                    "request": request,
                    "instructions": "Every turn must select exactly one available tool. Use the available tools to fulfill purchase requests. If a product is underspecified but searchable, search the catalogue first; do not ask the user for information the tools can provide. Use respond_to_customer only when the task is complete, blocked by a deterministic tool result, or genuinely requires user input. You never determine payment state. After PENDING or AMBIGUOUS, do not start another payment; observe the existing attempt. A rejected checkout is terminal for this request; explain the rejection and do not retry checkout.",
                    "tools": [tool for tool in TOOL_DEFINITIONS if tool["name"] in _allowed_tools(history)],
                    "history": history,
                }
            )
        except Exception as error:
            LOGGER.error("OpenRouter model failure: %s", _safe_error_message(error))
            return {"message": "I could not complete this request safely.", "history": history + [{"error": "Model request failed"}]}
        if not isinstance(action, dict):
            history.append({"error": "Model returned an invalid action"})
            continue
        name, arguments = action.get("tool"), action.get("arguments")
        if name not in _allowed_tools(history) or not isinstance(arguments, dict):
            history.append({"tool": name, "error": "Exactly one available tool call is required"})
            continue
        try:
            _validate_tool_arguments(name, arguments)
            result = getattr(tools, name)(**arguments)
        except (AttributeError, TypeError, ValueError) as error:
            result = {"error": str(error)}
        entry = {"tool": name, "arguments": arguments, "result": result}
        if isinstance(action.get("_assistant_message"), dict):
            entry["_assistant_message"] = action["_assistant_message"]
        if isinstance(action.get("_tool_call_id"), str):
            entry["tool_call_id"] = action["_tool_call_id"]
        history.append(entry)
        if name == "respond_to_customer":
            return {"message": result["message"], "history": history}
    return {"message": "I could not complete this request safely.", "history": history}


def _safe_error_message(error: Exception) -> str:
    """Keep provider diagnostics useful without copying credentials to logs."""
    message = str(error)
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", message)
    message = re.sub(r"(?i)(api[_ -]?key|secret|token|password)\s*[:=]\s*\S+", r"\1=<redacted>", message)
    return message[:1000]


def _validate_tool_arguments(name: str, arguments: dict) -> None:
    """Reject malformed JSON shapes before they reach a tool implementation."""
    if name == "respond_to_customer":
        if not isinstance(arguments.get("message"), str) or not arguments["message"].strip():
            raise ValueError("respond_to_customer.message must be a non-empty string")
        return
    if name != "create_cart":
        return
    items = arguments.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("create_cart.items must be a non-empty array")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("product_id"), str)
            or not item["product_id"]
            or isinstance(item.get("quantity"), bool)
            or not isinstance(item.get("quantity"), int)
            or item["quantity"] < 1
        ):
            raise ValueError("create_cart.items must contain product_id and quantity >= 1")


def _allowed_tools(history: list[dict]) -> set[str]:
    names = {tool["name"] for tool in TOOL_DEFINITIONS}
    for entry in history:
        result = entry.get("result", {})
        if entry.get("tool") == "start_checkout" and (
            result.get("status") in {"PENDING", "AMBIGUOUS"} or result.get("allowed") is False
        ):
            return {"respond_to_customer", "get_payment_status", "get_audit_timeline"}
    return names
