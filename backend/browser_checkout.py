"""Untrusted browser payment-reference correlation boundary."""

from psycopg.rows import dict_row

from backend.checkout import _append_audit
from backend.db import connect
from backend.resolver import _client_evidence, _resolve_attempt


WAITING_MESSAGE = "Payment is being confirmed"


def record_client_timeout(intent_id: str, customer_id: str, event: str = "timeout") -> dict:
    """Record a demo-only debit/timeout through the resolver's non-final evidence boundary."""
    if event not in {"timeout", "debit_reported"}:
        raise ValueError("unsupported client event")
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT pa.id AS attempt_id, pa.intent_id, pa.status, ci.customer_id "
            "FROM payment_attempts pa JOIN checkout_intents ci ON ci.id = pa.intent_id "
            "WHERE ci.id = %s FOR UPDATE OF pa",
            (intent_id,),
        )
        attempt = cursor.fetchone()
        if not attempt or attempt["customer_id"] != customer_id:
            return {"accepted": False, "message": WAITING_MESSAGE}
        if attempt["status"] != "PENDING":
            return {"accepted": False, "message": WAITING_MESSAGE, "status": attempt["status"]}
        result = _resolve_attempt(cursor, attempt["attempt_id"], _client_evidence(event))
    return {"accepted": True, **result, "message": "Payment is still being confirmed. Do not retry."}


def record_client_payment_reference(
    intent_id: str, customer_id: str, razorpay_order_id: str, razorpay_payment_id: str
) -> dict:
    """Record a browser reference without allowing it to resolve the payment."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.customer_id, pa.id AS attempt_id, pa.razorpay_order_id, "
            "pa.razorpay_payment_id, pa.status FROM checkout_intents ci "
            "JOIN payment_attempts pa ON pa.intent_id = ci.id WHERE ci.id = %s FOR UPDATE OF pa",
            (intent_id,),
        )
        attempt = cursor.fetchone()
        if not attempt or attempt["customer_id"] != customer_id:
            return {"accepted": False, "message": WAITING_MESSAGE}
        if (
            not isinstance(razorpay_payment_id, str)
            or not razorpay_payment_id
            or razorpay_order_id != attempt["razorpay_order_id"]
            or attempt["status"] not in {"PENDING", "AMBIGUOUS"}
        ):
            _append_audit(
                cursor,
                intent_id=intent_id,
                attempt_id=attempt["attempt_id"],
                event_type="CLIENT_REPORT_REJECTED",
                evidence_source="CLIENT_REPORTED",
                payload={"razorpay_order_id": razorpay_order_id},
            )
            return {"accepted": False, "message": WAITING_MESSAGE}
        idempotent = attempt["razorpay_payment_id"] == razorpay_payment_id
        if not idempotent:
            cursor.execute(
                "SELECT 1 FROM audit_events WHERE intent_id = %s AND type = 'CLIENT_REPORTED' "
                "AND payload_json->>'client_payment_id' = %s",
                (intent_id, razorpay_payment_id),
            )
            idempotent = cursor.fetchone() is not None
        if not idempotent and not attempt["razorpay_payment_id"]:
            cursor.execute("UPDATE payment_attempts SET razorpay_payment_id = %s WHERE id = %s", (razorpay_payment_id, attempt["attempt_id"]))
        _append_audit(
            cursor,
            intent_id=intent_id,
            attempt_id=attempt["attempt_id"],
            event_type="CLIENT_REPORTED",
            evidence_source="CLIENT_REPORTED",
            payload={"razorpay_order_id": razorpay_order_id, "client_payment_id": razorpay_payment_id,
                     "authoritative_payment_id": attempt["razorpay_payment_id"],
                     "discrepancy": bool(attempt["razorpay_payment_id"] and not idempotent)},
        )
    return {"accepted": True, "idempotent": idempotent, "message": WAITING_MESSAGE}
