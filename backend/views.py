"""Safe customer and operator projections over persisted payment records."""

import os

from psycopg.rows import dict_row

from backend.db import connect


MESSAGES = {
    "PENDING": "Payment is being confirmed. Do not retry.",
    "AMBIGUOUS": "Payment is still being confirmed. Do not retry.",
    "ABANDONED": "Payment is still being confirmed. Do not retry.",
    "CAPTURED": "Payment confirmed.",
    "FAILED": "Payment failed; you may try again.",
    "REVERSED": "Payment was reversed.",
    "REFUNDED": "Payment was refunded.",
}


def customer_intent(intent_id: str, customer_id: str | None = None) -> dict:
    """Return only the customer-safe state and existing checkout launch data."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        query = "SELECT ci.id, ci.customer_id, ci.mandate_id, ci.status, pa.razorpay_order_id, pa.razorpay_payment_id, c.total_paise, c.items_json, m.max_amount_paise, m.merchant_id FROM checkout_intents ci JOIN payment_attempts pa ON pa.intent_id = ci.id JOIN carts c ON c.id = ci.cart_id JOIN mandates m ON m.id = ci.mandate_id WHERE ci.id = %s"
        values = (intent_id,)
        if customer_id is not None:
            query += " AND ci.customer_id = %s"
            values += (customer_id,)
        cursor.execute(query, values)
        intent = cursor.fetchone()
        items = intent["items_json"] if intent else []
        products = {}
        if intent:
            product_ids = [item.get("product_id") for item in items if isinstance(item, dict) and item.get("product_id")]
            cursor.execute("SELECT p.id, p.name, p.category, p.price_paise, COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id WHERE p.id = ANY(%s)", (product_ids,))
            products = {product["id"]: product for product in cursor.fetchall()}
            cursor.execute(
                "SELECT sequence, type, evidence_source, payload_json, created_at FROM audit_events "
                "WHERE intent_id = %s ORDER BY sequence", (intent_id,)
            )
            timeline = [_safe_event(event) for event in cursor if event["type"] != "CLIENT_REPORT_REJECTED"]
    if not intent:
        return {"found": False}
    result = {
        "found": True, "intent_id": intent_id, "status": intent["status"],
        "message": MESSAGES.get(intent["status"], "Payment status is unavailable."),
        "order_id": intent["razorpay_order_id"], "payment_id": intent["razorpay_payment_id"],
        "cart_total_paise": intent["total_paise"], "tax_paise": 0,
        "mandate_id": intent["mandate_id"], "mandate_cap_paise": intent["max_amount_paise"], "merchant_id": intent["merchant_id"],
        "timeline": timeline,
        "items": [
            {**products[item["product_id"]], "quantity": item["quantity"]}
            for item in items if item.get("product_id") in products
        ],
    }
    if intent["status"] == "PENDING":
        result["checkout"] = {
            "intent_id": intent_id, "customer_id": intent["customer_id"],
            "order_id": intent["razorpay_order_id"], "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID"),
        }
    return result


def customer_transactions(customer_id: str) -> dict:
    """Return the customer's safe transaction history for selection after reload."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.id AS intent_id, ci.status, ci.created_at, pa.razorpay_order_id, "
            "pa.razorpay_payment_id, c.total_paise, c.items_json, m.merchant_id "
            "FROM checkout_intents ci JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "JOIN carts c ON c.id = ci.cart_id JOIN mandates m ON m.id = ci.mandate_id "
            "WHERE ci.customer_id = %s ORDER BY ci.created_at DESC", (customer_id,),
        )
        rows = cursor.fetchall()
        product_ids = {
            item.get("product_id") for row in rows for item in row["items_json"]
            if isinstance(item, dict) and item.get("product_id")
        }
        cursor.execute("SELECT id, name FROM products WHERE id = ANY(%s)", (list(product_ids),))
        names = {product["id"]: product["name"] for product in cursor.fetchall()}
    return {
        "customer_id": customer_id,
        "transactions": [
            {"intent_id": row["intent_id"], "status": row["status"],
             "created_at": row["created_at"], "order_id": row["razorpay_order_id"],
             "payment_id": row["razorpay_payment_id"], "amount_paise": row["total_paise"],
             "merchant_id": row["merchant_id"],
             "product": ", ".join(names[item["product_id"]] for item in row["items_json"]
                                    if item.get("product_id") in names) or "Checkout"}
            for row in rows
        ],
    }


def operator_intent(intent_id: str) -> dict:
    """Return operator identifiers, status, and a filtered evidence timeline."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.id, pa.id AS attempt_id, pa.status, pa.resolution_reason, "
            "pa.razorpay_order_id, pa.razorpay_payment_id, pa.last_authoritative_at, "
            "c.total_paise, m.max_amount_paise "
            "FROM checkout_intents ci JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "JOIN carts c ON c.id = ci.cart_id JOIN mandates m ON m.id = ci.mandate_id "
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
    return {**intent, "found": True, "timeline": timeline, "evidence": _evidence_summary(intent, timeline)}


def _safe_detail(payload: dict) -> dict:
    return {key: payload[key] for key in (
        "status", "reason", "reconciliation_required", "provider_event", "provider_status",
        "matched_order_id", "authoritative_payment_id", "signature_verified", "client_payment_id", "discrepancy",
    )
            if isinstance(payload, dict) and key in payload}


def _evidence_summary(intent: dict, timeline: list[dict]) -> dict:
    resolved = next((event["detail"] for event in reversed(timeline) if event["type"] == "ATTEMPT_RESOLVED"), {})
    webhook = next((event["detail"] for event in reversed(timeline) if event["type"] == "WEBHOOK_RECEIVED"), {})
    client = next((event["detail"] for event in reversed(timeline) if event["type"] == "CLIENT_REPORTED"), {})
    contradiction = next(
        (event["detail"].get("reason") for event in reversed(timeline)
         if event["type"] in {"RESOLUTION_EXCEPTION", "WEBHOOK_CONTRADICTION"} and event["detail"].get("reason")),
        None,
    )
    return {
        "current_status": intent["status"], "resolution_reason": intent["resolution_reason"],
        "provider_event": resolved.get("provider_event") or webhook.get("provider_event"),
        "provider_status": resolved.get("provider_status") or webhook.get("provider_status"),
        "matched_order_id": resolved.get("matched_order_id") or webhook.get("matched_order_id"),
        "authoritative_payment_id": intent["razorpay_payment_id"],
        "signature_verified": webhook.get("signature_verified"), "contradiction": contradiction,
        "client_payment_id": client.get("client_payment_id"),
        "client_provider_discrepancy": bool(client.get("client_payment_id") and intent["razorpay_payment_id"] and client["client_payment_id"] != intent["razorpay_payment_id"]),
    }


def operator_transactions(query: str = "") -> dict:
    """List persisted payment attempts for operator selection/search."""
    query = (query or "").strip()
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT ci.id AS intent_id, pa.id AS attempt_id, ci.status, ci.created_at, "
            "pa.razorpay_order_id, pa.razorpay_payment_id, c.total_paise "
            "FROM checkout_intents ci JOIN payment_attempts pa ON pa.intent_id = ci.id "
            "JOIN carts c ON c.id = ci.cart_id "
            "WHERE (%s = '' OR ci.id ILIKE %s OR pa.razorpay_order_id ILIKE %s "
            "OR pa.razorpay_payment_id ILIKE %s) ORDER BY ci.created_at DESC",
            (query, f"%{query}%", f"%{query}%", f"%{query}%"),
        )
        rows = cursor.fetchall()
    return {"transactions": [
        {"intent_id": row["intent_id"], "attempt_id": row["attempt_id"], "status": row["status"],
         "created_at": row["created_at"], "order_id": row["razorpay_order_id"],
         "payment_id": row["razorpay_payment_id"], "amount_paise": row["total_paise"]}
        for row in rows
    ]}


def merchant_metrics() -> dict:
    """Compact growth metrics derived from existing requests, carts, and attempts."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT COUNT(*) AS requests FROM audit_events WHERE type = 'CUSTOMER_MESSAGE' AND actor = 'AGENT'")
        requests = cursor.fetchone()["requests"]
        cursor.execute("SELECT COUNT(*) AS carts FROM carts")
        carts = cursor.fetchone()["carts"]
        cursor.execute("SELECT COUNT(*) AS checkout, COUNT(*) FILTER (WHERE ci.status = 'CAPTURED') AS captured, COALESCE(AVG(c.total_paise) FILTER (WHERE ci.status = 'CAPTURED'), 0) AS aov, COALESCE(SUM(c.total_paise) FILTER (WHERE ci.status = 'CAPTURED'), 0) AS revenue FROM checkout_intents ci JOIN carts c ON c.id = ci.cart_id")
        checkout = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) AS accepted FROM carts WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(items_json) AS item WHERE item->>'source' = 'recommendation')")
        accepted = cursor.fetchone()["accepted"]
        cursor.execute("SELECT COALESCE(SUM(p.price_paise * (item->>'quantity')::BIGINT), 0) AS revenue FROM checkout_intents ci JOIN carts c ON c.id = ci.cart_id CROSS JOIN LATERAL jsonb_array_elements(c.items_json) AS item JOIN products p ON p.id = item->>'product_id' WHERE ci.status = 'CAPTURED' AND item->>'source' = 'recommendation'")
        recommendation_revenue = cursor.fetchone()["revenue"]
        cursor.execute("SELECT COUNT(*) AS prevented FROM audit_events WHERE type = 'CHECKOUT_BLOCKED' OR (type = 'RESOLUTION_EXCEPTION' AND payload_json->>'reason' ILIKE '%unresolved%')")
        prevented = cursor.fetchone()["prevented"]
        cursor.execute("SELECT COUNT(*) AS resolved FROM audit_events WHERE type = 'ATTEMPT_RESOLVED' AND payload_json->>'previous_status' IN ('PENDING', 'AMBIGUOUS')")
        resolved = cursor.fetchone()["resolved"]
    return {"requests": requests, "recommendations_accepted": accepted, "recommendation_revenue_paise": recommendation_revenue, "carts": carts, "checkout_conversion": round(checkout["captured"] / carts, 3) if carts else 0, "average_order_value_paise": round(checkout["aov"]), "cross_sell_attachment_rate": round(accepted / carts, 3) if carts else 0, "captured_revenue_paise": checkout["revenue"], "duplicate_charges_prevented": prevented, "ambiguous_payments_resolved": resolved}


def _safe_event(event: dict) -> dict:
    return {
        "sequence": event["sequence"], "type": event["type"],
        "evidence_source": event["evidence_source"],
        "detail": _safe_detail(event["payload_json"]), "created_at": event["created_at"],
    }
