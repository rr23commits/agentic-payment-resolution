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
        cursor.execute(
            "INSERT INTO webhook_events (provider_event_id, payload_json, signature_valid) "
            "VALUES (%s, %s, TRUE) ON CONFLICT DO NOTHING RETURNING provider_event_id",
            (provider_event_id, Jsonb(payload)),
        )
        if not cursor.fetchone():
            return {"accepted": True, "idempotent": True}

        attempt = _find_attempt(cursor, order_id, payment_id)
        if attempt:
            _append_audit(
                cursor,
                intent_id=attempt["intent_id"],
                attempt_id=attempt["id"],
                event_type="WEBHOOK_RECEIVED",
                evidence_source="RAZORPAY_WEBHOOK",
                payload={"provider_event_id": provider_event_id, "event": payload.get("event")},
            )
            _resolve_attempt(
                cursor,
                attempt["id"],
                _provider_evidence(
                    "RAZORPAY_WEBHOOK", event=payload.get("event"),
                    order_id=order_id, payment_id=payment_id,
                ),
            )
        cursor.execute(
            "UPDATE webhook_events SET processed_at = CURRENT_TIMESTAMP WHERE provider_event_id = %s",
            (provider_event_id,),
        )
    return {"accepted": True, "idempotent": False, "matched": bool(attempt)}


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
    references = []
    values = []
    if order_id:
        references.append("pa.razorpay_order_id = %s")
        values.append(order_id)
    if payment_id:
        references.append("pa.razorpay_payment_id = %s")
        values.append(payment_id)
    if not references:
        return None
    cursor.execute(
        "SELECT pa.id, pa.intent_id FROM payment_attempts pa WHERE "
        + " OR ".join(references)
        + " LIMIT 1",
        values,
    )
    return cursor.fetchone()
