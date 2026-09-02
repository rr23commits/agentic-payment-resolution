"""Authoritative payment-state transitions and Razorpay reconciliation."""

from psycopg.rows import dict_row

from backend.checkout import _append_audit, _release_cart_stock
from backend.db import connect
from backend.razorpay import fetch_order_payments


FINAL_STATUSES = {"CAPTURED", "FAILED", "REVERSED", "REFUNDED"}
PROVIDER_SOURCES = {"RAZORPAY_WEBHOOK", "RAZORPAY_RECONCILIATION"}
_AUTHORITY = object()


def resolve_attempt(attempt_id: str, authoritative_evidence: dict) -> dict:
    """Apply one permitted payment transition from server-held evidence."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        return _resolve_attempt(cursor, attempt_id, authoritative_evidence)


def _provider_evidence(source: str, **evidence: str | None) -> dict:
    """Mark evidence assembled only after a verified webhook or provider API read."""
    return {"source": source, "_authority": _AUTHORITY, **evidence}


def _client_evidence(event: str) -> dict:
    """Mark a server-recorded client debit/timeout report as non-final evidence."""
    return {"source": "CLIENT_REPORTED", "event": event, "_authority": _AUTHORITY}


def _resolve_attempt(cursor, attempt_id: str, evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise ValueError("Payment evidence must be an object")
    cursor.execute(
        "SELECT * FROM payment_attempts WHERE id = %s FOR UPDATE",
        (attempt_id,),
    )
    attempt = cursor.fetchone()
    if not attempt:
        raise ValueError("Payment attempt does not exist")
    cursor.execute("SELECT id FROM checkout_intents WHERE id = %s FOR UPDATE", (attempt["intent_id"],))
    if not cursor.fetchone():
        raise ValueError("Payment intent does not exist")

    target, reason = _target_status(attempt, evidence)
    if target is None:
        return _exception(cursor, attempt, reason)
    payment_id = evidence.get("payment_id")
    if payment_id and attempt["razorpay_payment_id"] not in {None, payment_id}:
        cursor.execute(
            "SELECT 1 FROM audit_events WHERE intent_id = %s AND type = 'CLIENT_REPORTED' "
            "AND payload_json->>'client_payment_id' = %s",
            (attempt["intent_id"], attempt["razorpay_payment_id"]),
        )
        if not cursor.fetchone():
            return _exception(cursor, attempt, "Provider payment reference conflicts with the attempt")
    if payment_id:
        cursor.execute(
            "SELECT 1 FROM payment_attempts WHERE razorpay_payment_id = %s AND id <> %s",
            (payment_id, attempt_id),
        )
        if cursor.fetchone():
            return _exception(cursor, attempt, "Provider payment reference belongs to another attempt")
    if target == attempt["status"]:
        return {"attempt_id": attempt_id, "status": target, "idempotent": True}
    if not _allowed_transition(attempt["status"], target):
        return _exception(cursor, attempt, f"{attempt['status']} cannot transition to {target}")
    if target == "FAILED" and attempt.get("stock_reserved"):
        cursor.execute("SELECT cart_id FROM checkout_intents WHERE id = %s", (attempt["intent_id"],))
        _release_cart_stock(cursor, cursor.fetchone()["cart_id"])
    canonical_payment_id = payment_id if evidence["source"] in PROVIDER_SOURCES else attempt["razorpay_payment_id"]
    cursor.execute(
        "UPDATE payment_attempts SET status = %s, stock_reserved = FALSE, razorpay_payment_id = %s, "
        "last_authoritative_at = CURRENT_TIMESTAMP, resolution_reason = %s WHERE id = %s",
        (target, canonical_payment_id, reason, attempt_id),
    )
    cursor.execute("UPDATE checkout_intents SET status = %s WHERE id = %s", (target, attempt["intent_id"]))
    _append_audit(
        cursor,
        intent_id=attempt["intent_id"],
        attempt_id=attempt_id,
        event_type="ATTEMPT_RESOLVED",
        evidence_source=evidence["source"],
        payload={"status": target, "previous_status": attempt["status"], "reason": reason,
                 "provider_event": evidence.get("event"),
                 "provider_status": evidence.get("status") or (evidence.get("event") or "").removeprefix("payment."),
                 "matched_order_id": evidence.get("order_id"),
                 "authoritative_payment_id": payment_id,
                 "signature_verified": evidence.get("source") == "RAZORPAY_WEBHOOK"},
    )
    return {"attempt_id": attempt_id, "status": target, "idempotent": False}


def reconcile_status(attempt_id: str) -> dict:
    """Read Razorpay's current payment status without creating or retrying an order."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT razorpay_order_id FROM payment_attempts WHERE id = %s", (attempt_id,))
        attempt = cursor.fetchone()
    if not attempt:
        raise ValueError("Payment attempt does not exist")
    if not attempt["razorpay_order_id"]:
        payments = []
    else:
        payments = fetch_order_payments(attempt["razorpay_order_id"])
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM payment_attempts WHERE id = %s FOR UPDATE", (attempt_id,))
        current = cursor.fetchone()
        cursor.execute("SELECT id FROM checkout_intents WHERE id = %s FOR UPDATE", (current["intent_id"],))
        if current["status"] in FINAL_STATUSES:
            return {"attempt_id": attempt_id, "status": current["status"], "idempotent": True}
        payment = _matching_payment(payments, current["razorpay_payment_id"])
        if not payment:
            _append_audit(
                cursor,
                intent_id=current["intent_id"], attempt_id=attempt_id,
                event_type="RECONCILIATION_CHECKED",
                evidence_source="RAZORPAY_RECONCILIATION",
                payload={"observed": False, **({"reason": "Provider order ID is not persisted"} if not current["razorpay_order_id"] else {})},
            )
            return {"attempt_id": attempt_id, "status": current["status"], "observed": False}
        evidence = _provider_evidence(
            "RAZORPAY_RECONCILIATION", status=payment.get("status"),
            order_id=payment.get("order_id"), payment_id=payment.get("id"),
        )
        _append_audit(
            cursor,
            intent_id=current["intent_id"],
            attempt_id=attempt_id,
            event_type="RECONCILIATION_CHECKED",
            evidence_source="RAZORPAY_RECONCILIATION",
            payload={"payment_id": evidence["payment_id"], "status": evidence["status"]},
        )
        return _resolve_attempt(cursor, attempt_id, evidence)


def _target_status(attempt: dict, evidence: dict) -> tuple[str | None, str]:
    source = evidence.get("source") if isinstance(evidence, dict) else None
    if evidence.get("_authority") is not _AUTHORITY:
        return None, "Evidence was not assembled by a server authority boundary"
    if source == "CLIENT_REPORTED":
        if evidence.get("event") in {"debit_reported", "timeout"} and attempt["status"] == "PENDING":
            return "AMBIGUOUS", evidence["event"]
        return None, "Client evidence cannot resolve this payment"
    if source not in PROVIDER_SOURCES:
        return None, "Evidence is not verified Razorpay evidence"
    if evidence.get("order_id") != attempt["razorpay_order_id"]:
        return None, "Provider order reference does not match the attempt"
    status = evidence.get("status") or evidence.get("event", "").removeprefix("payment.")
    targets = {"captured": "CAPTURED", "failed": "FAILED", "reversed": "REVERSED", "refunded": "REFUNDED"}
    if status not in targets:
        return None, "Provider evidence has no final payment status"
    return targets[status], f"Razorpay {status}"


def _allowed_transition(current: str, target: str) -> bool:
    if current in {"PENDING", "AMBIGUOUS"}:
        return target in FINAL_STATUSES | {"AMBIGUOUS"}
    if current == "ABANDONED":
        return target in FINAL_STATUSES
    return current == "CAPTURED" and target in {"REVERSED", "REFUNDED"}


def _exception(cursor, attempt: dict, reason: str) -> dict:
    _append_audit(
        cursor,
        intent_id=attempt["intent_id"],
        attempt_id=attempt["id"],
        event_type="RESOLUTION_EXCEPTION",
        evidence_source="RESOLVER",
        payload={"reason": reason, "reconciliation_required": True},
    )
    return {
        "attempt_id": attempt["id"],
        "status": attempt["status"],
        "changed": False,
        "reconciliation_required": True,
    }


def _matching_payment(payments: list[dict], payment_id: str | None) -> dict | None:
    if payment_id:
        return next((payment for payment in payments if payment.get("id") == payment_id), None)
    return next(
        (payment for payment in payments if payment.get("status") in {"captured", "refunded", "failed"}),
        None,
    )
