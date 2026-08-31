"""Authoritative payment-state transitions and Razorpay reconciliation."""

from psycopg.rows import dict_row

from backend.checkout import _append_audit
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
        "SELECT ci.id AS intent_id FROM checkout_intents ci "
        "JOIN payment_attempts pa ON pa.intent_id = ci.id WHERE pa.id = %s FOR UPDATE",
        (attempt_id,),
    )
    intent = cursor.fetchone()
    if not intent:
        raise ValueError("Payment attempt does not exist")
    cursor.execute(
        "SELECT * FROM payment_attempts WHERE id = %s FOR UPDATE",
        (attempt_id,),
    )
    attempt = cursor.fetchone()
    attempt["intent_id"] = intent["intent_id"]

    target, reason = _target_status(attempt, evidence)
    if target is None:
        return _exception(cursor, attempt, reason)
    payment_id = evidence.get("payment_id")
    if payment_id and attempt["razorpay_payment_id"] not in {None, payment_id}:
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
    cursor.execute(
        "UPDATE payment_attempts SET status = %s, razorpay_payment_id = COALESCE(%s, razorpay_payment_id), "
        "last_authoritative_at = CURRENT_TIMESTAMP, resolution_reason = %s WHERE id = %s",
        (target, payment_id, reason, attempt_id),
    )
    cursor.execute("UPDATE checkout_intents SET status = %s WHERE id = %s", (target, attempt["intent_id"]))
    _append_audit(
        cursor,
        intent_id=attempt["intent_id"],
        attempt_id=attempt_id,
        event_type="ATTEMPT_RESOLVED",
        evidence_source=evidence["source"],
        payload={"status": target, "reason": reason},
    )
    return {"attempt_id": attempt_id, "status": target, "idempotent": False}


def reconcile_status(attempt_id: str) -> dict:
    """Read Razorpay's current payment status without creating or retrying an order."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT id, intent_id, razorpay_order_id, razorpay_payment_id, status "
            "FROM payment_attempts WHERE id = %s",
            (attempt_id,),
        )
        attempt = cursor.fetchone()
    if not attempt:
        raise ValueError("Payment attempt does not exist")
    if not attempt["razorpay_order_id"]:
        with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT id, intent_id, status FROM payment_attempts WHERE id = %s FOR UPDATE",
                (attempt_id,),
            )
            current = cursor.fetchone()
            _append_audit(
                cursor, intent_id=current["intent_id"], attempt_id=attempt_id,
                event_type="RECONCILIATION_CHECKED",
                evidence_source="RAZORPAY_RECONCILIATION",
                payload={"observed": False, "reason": "Provider order ID is not persisted"},
            )
            return {"attempt_id": attempt_id, "status": current["status"], "observed": False}
    payments = fetch_order_payments(attempt["razorpay_order_id"])
    payment = _matching_payment(payments, attempt["razorpay_payment_id"])
    if not payment:
        with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT id, intent_id, status FROM payment_attempts WHERE id = %s FOR UPDATE",
                (attempt_id,),
            )
            current = cursor.fetchone()
            _append_audit(
                cursor,
                intent_id=current["intent_id"], attempt_id=attempt_id,
                event_type="RECONCILIATION_CHECKED",
                evidence_source="RAZORPAY_RECONCILIATION",
                payload={"observed": False},
            )
            return {"attempt_id": attempt_id, "status": current["status"], "observed": False}

    evidence = _provider_evidence(
        "RAZORPAY_RECONCILIATION", status=payment.get("status"),
        order_id=payment.get("order_id"), payment_id=payment.get("id"),
    )
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        _append_audit(
            cursor,
            intent_id=attempt["intent_id"],
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
    targets = {"authorized": "CAPTURED", "captured": "CAPTURED", "failed": "FAILED", "reversed": "REVERSED", "refunded": "REFUNDED"}
    if status not in targets:
        return None, "Provider evidence has no final payment status"
    return targets[status], f"Razorpay {status}"


def _allowed_transition(current: str, target: str) -> bool:
    if current in {"PENDING", "AMBIGUOUS"}:
        return target in FINAL_STATUSES | {"AMBIGUOUS"}
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
