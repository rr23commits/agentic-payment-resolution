"""Idempotent checkout creation at the Razorpay order boundary."""

import os
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.catalogue import _validate_purchase
from backend.db import connect
from backend.razorpay import create_order


def start_checkout(
    cart_id: str, mandate_id: str, client_request_id: str, *, customer_id: str
) -> dict:
    """Create one Test Mode order, or return/refuse the existing safe result."""
    if not client_request_id:
        raise ValueError("client_request_id is required")
    final_status = "PENDING"
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        # Serializes this idempotency key before an external order can be created.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{customer_id}:{client_request_id}",),
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"cart:{customer_id}:{cart_id}",),
        )
        cursor.execute(
            "SELECT ci.id AS intent_id, ci.status, pa.razorpay_order_id FROM checkout_intents ci "
            "JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "WHERE ci.customer_id = %s AND ci.client_request_id = %s",
            (customer_id, client_request_id),
        )
        existing = cursor.fetchone()
        if existing:
            if existing["status"] in {"CREATED", "AMBIGUOUS"} and not existing["razorpay_order_id"]:
                return {
                    "allowed": False,
                    "idempotent": True,
                    "intent_id": existing["intent_id"],
                    "status": existing["status"],
                    "message": "Checkout is still being created. Do not retry.",
                }
            return {
                "allowed": True,
                "idempotent": True,
                "intent_id": existing["intent_id"],
                "order_id": existing["razorpay_order_id"],
                "customer_id": customer_id,
                "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID"),
                "status": existing["status"],
            }

        cursor.execute(
            "SELECT ci.id AS intent_id, pa.id AS attempt_id FROM checkout_intents ci "
            "JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "WHERE ci.customer_id = %s AND ci.cart_id = %s "
            "AND pa.status <> 'FAILED'",
            (customer_id, cart_id),
        )
        active_attempt = cursor.fetchone()
        if active_attempt:
            _append_audit(
                cursor,
                intent_id=active_attempt["intent_id"],
                attempt_id=active_attempt["attempt_id"],
                event_type="CHECKOUT_BLOCKED",
                payload={"reason": "Existing payment has not reached verified FAILED state"},
            )
            return {
                "allowed": False,
                "reasons": ["Existing payment has not reached verified FAILED state"],
                "cart_id": cart_id,
            }

        validation = _validate_purchase(cursor, cart_id, mandate_id, customer_id)
        if not validation["allowed"]:
            return validation

        intent_id = f"intent_{uuid4().hex}"
        attempt_id = f"attempt_{uuid4().hex}"
        cursor.execute(
            "INSERT INTO checkout_intents "
            "(id, customer_id, mandate_id, cart_id, client_request_id, status) "
            "VALUES (%s, %s, %s, %s, %s, 'CREATED')",
            (intent_id, customer_id, mandate_id, cart_id, client_request_id),
        )
        cursor.execute(
            "INSERT INTO payment_attempts (id, intent_id, razorpay_order_id, status) "
            "VALUES (%s, %s, NULL, 'CREATED')",
            (attempt_id, intent_id),
        )
        _append_audit(
            cursor,
            intent_id=intent_id,
            attempt_id=attempt_id,
            event_type="CHECKOUT_INTENT_CREATED",
            payload={"cart_id": cart_id, "mandate_id": mandate_id},
        )
        total_paise = _cart_total(cursor, cart_id)
    try:
        order_id = create_order(total_paise, intent_id)
    except Exception as error:
        with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "UPDATE payment_attempts SET status = 'AMBIGUOUS', resolution_reason = %s "
                "WHERE id = %s AND status = 'CREATED' RETURNING intent_id",
                ("Razorpay order creation outcome is unknown", attempt_id),
            )
            updated = cursor.fetchone()
            if updated:
                cursor.execute(
                    "UPDATE checkout_intents SET status = 'AMBIGUOUS' WHERE id = %s",
                    (updated["intent_id"],),
                )
                _append_audit(
                    cursor,
                    intent_id=updated["intent_id"], attempt_id=attempt_id,
                    event_type="CHECKOUT_PROVIDER_FAILURE",
                    payload={"reason": "Razorpay order creation outcome is unknown"},
                )
        return {
            "allowed": False, "intent_id": intent_id, "status": "AMBIGUOUS",
            "message": "Payment creation could not be confirmed. Do not retry.",
        }

    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"order:{order_id}",),
        )
        cursor.execute(
            "UPDATE payment_attempts SET razorpay_order_id = %s, status = 'PENDING' "
            "WHERE id = %s AND status = 'CREATED' RETURNING intent_id",
            (order_id, attempt_id),
        )
        updated = cursor.fetchone()
        if not updated:
            return {
                "allowed": False, "intent_id": intent_id, "status": "AMBIGUOUS",
                "message": "Payment creation could not be confirmed. Do not retry.",
            }
        cursor.execute(
            "UPDATE checkout_intents SET status = 'PENDING' WHERE id = %s",
            (intent_id,),
        )
        _append_audit(
            cursor,
            intent_id=intent_id,
            attempt_id=attempt_id,
            event_type="RAZORPAY_ORDER_CREATED",
            payload={"razorpay_order_id": order_id},
        )
        from backend.webhooks import process_pending_webhooks

        process_pending_webhooks(cursor, order_id)
        cursor.execute("SELECT status FROM checkout_intents WHERE id = %s", (intent_id,))
        final_status = cursor.fetchone()["status"]
    return {
        "allowed": True,
        "idempotent": False,
        "intent_id": intent_id,
        "order_id": order_id,
        "customer_id": customer_id,
        "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID"),
        "status": final_status,
    }


def _cart_total(cursor, cart_id: str) -> int:
    cursor.execute("SELECT total_paise FROM carts WHERE id = %s", (cart_id,))
    return cursor.fetchone()["total_paise"]


def _append_audit(
    cursor,
    *,
    intent_id: str,
    attempt_id: str,
    event_type: str,
    payload: dict,
    evidence_source: str = "CHECKOUT",
) -> None:
    # Every intent event uses this lock, so sequence allocation cannot race concurrent webhook/browser work.
    cursor.execute("SELECT id FROM checkout_intents WHERE id = %s FOR UPDATE", (intent_id,))
    cursor.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
        "FROM audit_events WHERE intent_id = %s",
        (intent_id,),
    )
    sequence = cursor.fetchone()["next_sequence"]
    cursor.execute(
        "INSERT INTO audit_events "
        "(id, intent_id, attempt_id, sequence, type, actor, evidence_source, payload_json) "
        "VALUES (%s, %s, %s, %s, %s, 'SERVER', %s, %s)",
        (
            f"audit_{uuid4().hex}",
            intent_id,
            attempt_id,
            sequence,
            event_type,
            evidence_source,
            Jsonb(payload),
        ),
    )
