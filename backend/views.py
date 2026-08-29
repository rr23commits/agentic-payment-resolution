"""Safe customer and operator projections over persisted payment records."""

import os

from psycopg.rows import dict_row

from backend.db import connect


MESSAGES = {
    "PENDING": "Payment is being confirmed. Do not retry.",
    "AMBIGUOUS": "Payment is still being confirmed. Do not retry.",
    "CAPTURED": "Payment confirmed.",
    "FAILED": "Payment failed; you may try again.",
    "REVERSED": "Payment was reversed.",
    "REFUNDED": "Payment was refunded.",
}


def customer_intent(intent_id: str) -> dict:
    """Return only the customer-safe state and existing checkout launch data."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.id, ci.customer_id, ci.status, pa.razorpay_order_id, c.total_paise, "
            "m.max_amount_paise FROM checkout_intents ci "
            "JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "JOIN carts c ON c.id = ci.cart_id JOIN mandates m ON m.id = ci.mandate_id "
            "WHERE ci.id = %s",
            (intent_id,),
        )
        intent = cursor.fetchone()
    if not intent:
        return {"found": False}
    result = {
        "found": True, "intent_id": intent_id, "status": intent["status"],
        "message": MESSAGES.get(intent["status"], "Payment status is unavailable."),
        "cart_total_paise": intent["total_paise"], "mandate_cap_paise": intent["max_amount_paise"],
    }
    if intent["status"] == "PENDING":
        result["checkout"] = {
            "intent_id": intent_id, "customer_id": intent["customer_id"],
            "order_id": intent["razorpay_order_id"], "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID"),
        }
    return result


def operator_intent(intent_id: str) -> dict:
    """Return operator identifiers, status, and a filtered evidence timeline."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.id, pa.id AS attempt_id, pa.status, pa.resolution_reason "
            "FROM checkout_intents ci JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "WHERE ci.id = %s",
            (intent_id,),
        )
        intent = cursor.fetchone()
        if not intent:
            return {"found": False}
        cursor.execute(
            "SELECT sequence, type, evidence_source, payload_json, created_at FROM audit_events "
            "WHERE intent_id = %s ORDER BY sequence",
            (intent_id,),
        )
        timeline = [
            {"sequence": event["sequence"], "type": event["type"],
             "evidence_source": event["evidence_source"], "detail": _safe_detail(event["payload_json"]),
             "created_at": event["created_at"]}
            for event in cursor
        ]
    return {**intent, "found": True, "timeline": timeline}


def _safe_detail(payload: dict) -> dict:
    return {key: payload[key] for key in ("status", "reason", "reconciliation_required")
            if isinstance(payload, dict) and key in payload}
