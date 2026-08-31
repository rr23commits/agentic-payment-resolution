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
                    "instructions": "Every turn must select exactly one available tool. Use the available tools to fulfill purchase requests. Treat numbers in requests as purchase quantities, never as catalogue-result counts; preserve each category quantity when creating a cart and say explicitly when stock cannot fulfill it. If a product is underspecified but searchable, search the catalogue first; for multiple requested categories, search each category separately with at most 4 results. Recommend products and wait for the customer-facing selection flow; never change a mandate. Use respond_to_customer only when the task is complete, blocked by a deterministic tool result, or genuinely requires user input. You never determine payment state. After PENDING or AMBIGUOUS, do not start another payment; observe the existing attempt. A rejected checkout is terminal for this request; explain the rejection and do not retry checkout.",
                    "tools": [tool for tool in TOOL_DEFINITIONS if tool["name"] in _allowed_tools(history)],
                    "history": _model_history(history),
                }
            )
        except Exception as error:
            LOGGER.error("OpenRouter model failure: %s", _safe_error_message(error))
            return {"message": "I could not complete this request safely.", "history": history + [{"error": "Model request failed"}]}
        if not isinstance(action, dict):
            history.append({"error": "Model returned an invalid action"})
            continue
        if _terminal_response(action, history):
            return {"message": action["final"], "history": history}
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
            intent_id = next(
                (item["result"]["intent_id"] for item in reversed(history)
                 if isinstance(item.get("result"), dict) and item["result"].get("intent_id")),
                None,
            )
            record_message = getattr(tools, "record_customer_message", None)
            if callable(record_message):
                record_message(result["message"], intent_id)
            return {"message": result["message"], "history": history}
    return {"message": "I could not complete this request safely.", "history": history}


def _safe_error_message(error: Exception) -> str:
    """Keep provider diagnostics useful without copying credentials to logs."""
    message = str(error)
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", message)
    message = re.sub(r"(?i)(api[_ -]?key|secret|token|password)\s*[:=]\s*\S+", r"\1=<redacted>", message)
    return message[:1000]


def _model_history(history: list[dict]) -> list[dict]:
    """Send only task data to providers; payment evidence and audit payloads stay local."""
    allowed = {
        "search_catalogue": lambda value: value if isinstance(value, list) else [],
        "get_product_details": lambda value: value if isinstance(value, dict) else None,
        "create_cart": lambda value: {key: value[key] for key in ("cart_id", "total_paise") if isinstance(value, dict) and key in value},
        "get_mandate": lambda value: {key: value[key] for key in ("id", "max_amount_paise", "allowed_categories_json") if isinstance(value, dict) and key in value},
        "validate_purchase": lambda value: {key: value[key] for key in ("allowed", "reasons", "cart_id", "mandate_id", "cart_total_paise", "mandate_cap_paise") if isinstance(value, dict) and key in value},
        "start_checkout": lambda value: {key: value[key] for key in ("allowed", "intent_id", "status", "message", "reasons") if isinstance(value, dict) and key in value},
        "get_payment_status": lambda value: {key: value[key] for key in ("found", "status", "message") if isinstance(value, dict) and key in value},
        "get_audit_timeline": lambda value: [],
    }
    result = []
    for entry in history:
        item = {key: entry[key] for key in ("tool", "tool_call_id", "_assistant_message", "error") if key in entry}
        item["result"] = allowed.get(entry.get("tool"), lambda _value: None)(entry.get("result"))
        result.append(item)
    return result


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


def _terminal_response(action: dict, history: list[dict]) -> bool:
    """Accept provider text only after a successful checkout tool result."""
    final = action.get("final")
    if set(action) != {"final"} or not isinstance(final, str) or not final.strip() or not history:
        return False
    result = history[-1].get("result")
    return history[-1].get("tool") == "start_checkout" and isinstance(result, dict) and result.get("allowed") is True
