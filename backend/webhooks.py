"""Verified Razorpay webhook ingestion; payment resolution remains a later boundary."""

import hashlib
import hmac
import json
import os

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.checkout import _append_audit
from backend.db import connect
from backend.resolver import _provider_evidence, _resolve_attempt


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Check Razorpay's HMAC against the exact bytes received from the network."""
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def ingest_webhook(body: bytes, signature: str | None, provider_event_id: str | None) -> dict:
    """Persist one verified provider event and its correlation evidence."""
    if not verify_signature(body, signature):
        return {"accepted": False, "reason": "invalid signature"}
    if not provider_event_id:
        return {"accepted": False, "reason": "missing event id"}
    try:
        payload = json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {"accepted": False, "reason": "invalid JSON"}
    if not isinstance(payload, dict):
        return {"accepted": False, "reason": "invalid JSON"}

    order_id, payment_id = _references(payload)
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        if order_id:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"order:{order_id}",),
            )
        cursor.execute(
            "INSERT INTO webhook_events (provider_event_id, payload_json, signature_valid) "
            "VALUES (%s, %s, TRUE) ON CONFLICT DO NOTHING RETURNING provider_event_id",
            (provider_event_id, Jsonb(payload)),
        )
        if not cursor.fetchone():
            return {"accepted": True, "idempotent": True}

        attempt = _find_attempt(cursor, order_id, payment_id)
        if attempt:
            cursor.execute("SELECT id FROM payment_attempts WHERE id = %s FOR UPDATE", (attempt["id"],))
            _append_audit(
                cursor,
                intent_id=attempt["intent_id"],
                attempt_id=attempt["id"],
                event_type="WEBHOOK_RECEIVED",
                evidence_source="RAZORPAY_WEBHOOK",
                payload={"provider_event_id": provider_event_id, "event": payload.get("event")},
            )
            _resolve_attempt(cursor, attempt["id"], _provider_evidence(
                "RAZORPAY_WEBHOOK", event=payload.get("event"),
                order_id=order_id, payment_id=payment_id,
            ))
            _mark_processed(cursor, provider_event_id)
        elif _references_contradict(cursor, order_id, payment_id):
            _append_global_webhook_audit(cursor, provider_event_id, order_id, payment_id)
            _mark_processed(cursor, provider_event_id)
        elif not order_id:
            _mark_processed(cursor, provider_event_id)
    return {"accepted": True, "idempotent": False, "matched": bool(attempt)}


def process_pending_webhooks(cursor, order_id: str) -> None:
    """Resolve signed events that arrived before local order persistence committed."""
    cursor.execute(
        "SELECT provider_event_id, payload_json FROM webhook_events "
        "WHERE processed_at IS NULL AND "
        "(payload_json #>> '{payload,payment,entity,order_id}' = %s "
        "OR payload_json #>> '{payload,order,entity,id}' = %s) FOR UPDATE",
        (order_id, order_id),
    )
    for event in cursor.fetchall():
        payload = event["payload_json"]
        payment_order, payment_id = _references(payload)
        attempt = _find_attempt(cursor, payment_order, payment_id)
        if not attempt:
            if _references_contradict(cursor, payment_order, payment_id):
                _append_global_webhook_audit(cursor, event["provider_event_id"], payment_order, payment_id)
                _mark_processed(cursor, event["provider_event_id"])
            continue
        cursor.execute("SELECT id FROM payment_attempts WHERE id = %s FOR UPDATE", (attempt["id"],))
        _append_audit(
            cursor, intent_id=attempt["intent_id"], attempt_id=attempt["id"],
            event_type="WEBHOOK_RECEIVED", evidence_source="RAZORPAY_WEBHOOK",
            payload={"provider_event_id": event["provider_event_id"], "event": payload.get("event")},
        )
        _resolve_attempt(cursor, attempt["id"], _provider_evidence(
            "RAZORPAY_WEBHOOK", event=payload.get("event"),
            order_id=payment_order, payment_id=payment_id,
        ))
        _mark_processed(cursor, event["provider_event_id"])


def _mark_processed(cursor, provider_event_id: str) -> None:
    cursor.execute(
        "UPDATE webhook_events SET processed_at = CURRENT_TIMESTAMP WHERE provider_event_id = %s",
        (provider_event_id,),
    )


def _references(payload: dict) -> tuple[str | None, str | None]:
    entities = payload.get("payload", {})
    if not isinstance(entities, dict):
        return None, None
    payment = entities.get("payment", {})
    payment = payment.get("entity", {}) if isinstance(payment, dict) else {}
    order = entities.get("order", {})
    order = order.get("entity", {}) if isinstance(order, dict) else {}
    return (
        payment.get("order_id") or order.get("id"),
        payment.get("id"),
    )


def _find_attempt(cursor, order_id: str | None, payment_id: str | None) -> dict | None:
    if not order_id and not payment_id:
        return None
    if order_id:
        cursor.execute("SELECT id, intent_id FROM payment_attempts WHERE razorpay_order_id = %s", (order_id,))
        by_order = cursor.fetchone()
    else:
        by_order = None
    if payment_id:
        cursor.execute("SELECT id, intent_id FROM payment_attempts WHERE razorpay_payment_id = %s", (payment_id,))
        by_payment = cursor.fetchone()
    else:
        by_payment = None
    if by_order and by_payment and by_order["id"] != by_payment["id"]:
        return None
    return by_order or by_payment


def _references_contradict(cursor, order_id: str | None, payment_id: str | None) -> bool:
    if not order_id or not payment_id:
        return False
    cursor.execute("SELECT id FROM payment_attempts WHERE razorpay_order_id = %s", (order_id,))
    by_order = cursor.fetchone()
    cursor.execute("SELECT id FROM payment_attempts WHERE razorpay_payment_id = %s", (payment_id,))
    by_payment = cursor.fetchone()
    return bool(by_order and by_payment and by_order["id"] != by_payment["id"])


def _append_global_webhook_audit(cursor, event_id: str, order_id: str | None, payment_id: str | None) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('audit:preintent', 0))")
    cursor.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM audit_events WHERE intent_id IS NULL")
    cursor.execute(
        "INSERT INTO audit_events (id, intent_id, attempt_id, sequence, type, actor, evidence_source, payload_json) "
        "VALUES (%s, NULL, NULL, %s, 'WEBHOOK_CONTRADICTION', 'SERVER', 'RAZORPAY_WEBHOOK', %s)",
        (f"audit_{event_id}", cursor.fetchone()["next_sequence"], Jsonb({"provider_event_id": event_id, "order_id": order_id, "payment_id": payment_id, "reason": "Order and payment references belong to different attempts"})),
    )
