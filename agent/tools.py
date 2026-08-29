"""Customer-scoped allowlist for an agent; it never exposes payment authority."""

from backend.catalogue import (
    create_cart,
    get_mandate,
    get_product_details,
    search_catalogue,
    validate_purchase,
)
from backend.checkout import start_checkout
from backend.db import connect


def _parameters(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object", "properties": properties,
        "required": required or [], "additionalProperties": False,
    }


CREATE_CART_PARAMETERS = _parameters(
    {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["product_id", "quantity"],
                "additionalProperties": False,
            },
        }
    },
    ["items"],
)


TOOL_DEFINITIONS = [
    {"name": "respond_to_customer", "description": "Return the final customer-facing message and end the request.", "parameters": _parameters({
        "message": {"type": "string"},
    }, ["message"])},
    {"name": "search_catalogue", "description": "Find purchasable catalogue products.", "parameters": _parameters({
        "query": {"type": "string"}, "category": {"type": "string"},
    }, ["query"])},
    {"name": "get_product_details", "description": "Read current server-authoritative product details.", "parameters": _parameters({
        "product_id": {"type": "string"},
    }, ["product_id"])},
    {"name": "create_cart", "description": "Create a cart with server-calculated prices and totals.", "parameters": CREATE_CART_PARAMETERS},
    {"name": "get_mandate", "description": "Read the current customer's signed mandate constraints.", "parameters": _parameters({})},
    {"name": "validate_purchase", "description": "Check the cart against the mandate and return deterministic reasons.", "parameters": _parameters({
        "cart_id": {"type": "string"}, "mandate_id": {"type": "string"},
    }, ["cart_id", "mandate_id"])},
    {
        "name": "start_checkout",
        "description": "Create or return one checkout order. It cannot create another payment while an attempt is PENDING or AMBIGUOUS.",
        "parameters": _parameters({
            "cart_id": {"type": "string"}, "mandate_id": {"type": "string"}, "client_request_id": {"type": "string"},
        }, ["cart_id", "mandate_id", "client_request_id"]),
    },
    {"name": "get_payment_status", "description": "Read the safe customer-facing status of one payment.", "parameters": _parameters({
        "intent_id": {"type": "string"},
    }, ["intent_id"])},
    {"name": "get_audit_timeline", "description": "Read the customer-safe decision trail for one payment.", "parameters": _parameters({
        "intent_id": {"type": "string"},
    }, ["intent_id"])},
]


class AgentTools:
    """Bind every agent tool to the authenticated customer, never model input."""

    def __init__(self, customer_id: str):
        if not customer_id:
            raise ValueError("customer_id is required")
        self.customer_id = customer_id

    def respond_to_customer(self, message: str) -> dict:
        return {"message": message}

    def search_catalogue(self, query: str, category: str | None = None) -> list[dict]:
        return search_catalogue(query, category)

    def get_product_details(self, product_id: str) -> dict | None:
        return get_product_details(product_id)

    def create_cart(self, items: list[dict]) -> dict:
        return create_cart(items, customer_id=self.customer_id)

    def get_mandate(self) -> dict | None:
        return get_mandate(self.customer_id)

    def validate_purchase(self, cart_id: str, mandate_id: str) -> dict:
        return validate_purchase(cart_id, mandate_id, customer_id=self.customer_id)

    def start_checkout(self, cart_id: str, mandate_id: str, client_request_id: str) -> dict:
        return start_checkout(cart_id, mandate_id, client_request_id, customer_id=self.customer_id)

    def get_payment_status(self, intent_id: str) -> dict:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT ci.status FROM checkout_intents ci WHERE ci.id = %s AND ci.customer_id = %s",
                (intent_id, self.customer_id),
            )
            row = cursor.fetchone()
        if not row:
            return {"found": False}
        status = row[0]
        messages = {
            "PENDING": "Payment is being confirmed. Do not retry.",
            "AMBIGUOUS": "Payment is still being confirmed. Do not retry.",
            "CAPTURED": "Payment confirmed.",
            "FAILED": "Payment failed; you may try again.",
            "REVERSED": "Payment was reversed.",
            "REFUNDED": "Payment was refunded.",
        }
        return {"found": True, "intent_id": intent_id, "status": status, "message": messages.get(status, "Payment status is unavailable.")}

    def get_audit_timeline(self, intent_id: str) -> list[dict]:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT ae.sequence, ae.type, ae.evidence_source, ae.payload_json, ae.created_at "
                "FROM audit_events ae JOIN checkout_intents ci ON ci.id = ae.intent_id "
                "WHERE ae.intent_id = %s AND ci.customer_id = %s ORDER BY ae.sequence",
                (intent_id, self.customer_id),
            )
            return [
                {
                    "sequence": sequence,
                    "type": event_type,
                    "evidence_source": evidence_source,
                    "detail": _safe_detail(payload),
                    "created_at": created_at,
                }
                for sequence, event_type, evidence_source, payload, created_at in cursor
            ]


def tools_for(customer_id: str) -> AgentTools:
    """Create the only agent-facing capability set for an authenticated customer."""
    return AgentTools(customer_id)


def _safe_detail(payload: dict) -> dict:
    return {
        key: payload[key]
        for key in ("status", "reason", "reconciliation_required")
        if isinstance(payload, dict) and key in payload
    }
