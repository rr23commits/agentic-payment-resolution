"""Database-backed catalogue and mandate validation tools."""

import hashlib
import hmac
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.db import connect


MAX_ITEM_QUANTITY = 1000
MAX_CART_TOTAL_PAISE = 9_000_000_000_000_000_000
MANDATE_ROUNDING_PAISE = 10_000


def search_catalogue(query: str, category: str | None = None, limit: int = 4) -> list[dict]:
    """Return product summaries matching a customer query."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
        raise ValueError("Search limit must be between 1 and 4")
    filters = ["(name ILIKE %s OR description ILIKE %s)"]
    values: list[str] = [f"%{query}%", f"%{query}%"]
    if category:
        filters.append("category = %s")
        values.append(category)
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT id, name, description, category, price_paise "
            "FROM products WHERE restricted = FALSE AND " + " AND ".join(filters) + " ORDER BY name LIMIT %s",
            [*values, limit],
        )
        return list(cursor.fetchall())


def get_product_details(product_id: str) -> dict | None:
    """Return the current server-authoritative product record."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT id, name, description, category, price_paise FROM products WHERE id = %s AND restricted = FALSE", (product_id,))
        return cursor.fetchone()


def create_cart(items: list[dict], *, customer_id: str) -> dict:
    """Reload products and create a cart using only server-side prices."""
    quantities: dict[str, int] = defaultdict(int)
    for item in items:
        product_id, quantity = item.get("product_id"), item.get("quantity")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError("Each item needs a product_id")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Each item needs a positive integer quantity")
        quantities[product_id] += quantity
    if not quantities:
        raise ValueError("A cart needs at least one item")
    if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
        raise ValueError(f"Quantity cannot exceed {MAX_ITEM_QUANTITY}")

    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT id, price_paise, stock FROM products WHERE id = ANY(%s)",
            (list(quantities),),
        )
        products = {product["id"]: product for product in cursor.fetchall()}
        missing = sorted(set(quantities) - set(products))
        if missing:
            raise ValueError(f"Unknown product: {missing[0]}")
        for product_id, quantity in quantities.items():
            if quantity > products[product_id]["stock"]:
                raise ValueError(f"Insufficient stock for {product_id}")

        cart_items = [
            {"product_id": product_id, "quantity": quantity}
            for product_id, quantity in quantities.items()
        ]
        total_paise = sum(
            products[product_id]["price_paise"] * quantity
            for product_id, quantity in quantities.items()
        )
        if total_paise > MAX_CART_TOTAL_PAISE:
            raise ValueError("Cart total exceeds the supported maximum")
        cart_id = f"cart_{uuid4().hex}"
        cursor.execute(
            "INSERT INTO carts (id, customer_id, items_json, total_paise, status) "
            "VALUES (%s, %s, %s, %s, 'CREATED')",
            (cart_id, customer_id, Jsonb(cart_items), total_paise),
        )
    return {"cart_id": cart_id, "customer_id": customer_id, "total_paise": total_paise}


def get_mandate(customer_id: str) -> dict | None:
    """Return the customer's current mandate without exposing its signature."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT id, customer_id, merchant_id, agent_id, max_amount_paise, "
            "allowed_categories_json, expires_at FROM mandates m LEFT JOIN mandate_revisions mr ON mr.mandate_id = m.id "
            "WHERE customer_id = %s AND (mr.mandate_id IS NULL OR mr.active = TRUE) "
            "ORDER BY COALESCE(mr.version, 1) DESC, expires_at DESC LIMIT 1",
            (customer_id,),
        )
        return cursor.fetchone()


def _mandate_result(row: dict) -> dict:
    return {key: row[key] for key in (
        "id", "customer_id", "merchant_id", "agent_id", "max_amount_paise",
        "allowed_categories_json", "expires_at",
    )}


def update_mandate(
    customer_id: str, max_amount_paise: int, allowed_categories: list[str],
    expires_at: datetime, *, request_id: str,
) -> dict:
    """Create an immutable customer mandate revision, idempotently."""
    if not request_id or isinstance(max_amount_paise, bool) or not isinstance(max_amount_paise, int) or max_amount_paise < 0:
        raise ValueError("Invalid mandate update")
    if not isinstance(allowed_categories, list) or not allowed_categories or not all(isinstance(category, str) and category for category in allowed_categories):
        raise ValueError("At least one category is required")
    if expires_at <= datetime.now(timezone.utc):
        raise ValueError("Mandate expiry must be in the future")
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"mandate:{customer_id}",))
        cursor.execute("SELECT mandate_id FROM mandate_change_requests WHERE request_id = %s AND customer_id = %s", (request_id, customer_id))
        prior = cursor.fetchone()
        if prior:
            cursor.execute("SELECT * FROM mandates WHERE id = %s", (prior["mandate_id"],))
            return _mandate_result(cursor.fetchone())
        cursor.execute("SELECT m.*, COALESCE(mr.version, 1) AS mandate_version FROM mandates m LEFT JOIN mandate_revisions mr ON mr.mandate_id = m.id WHERE m.customer_id = %s AND (mr.mandate_id IS NULL OR mr.active = TRUE) ORDER BY COALESCE(mr.version, 1) DESC, m.expires_at DESC LIMIT 1 FOR UPDATE OF m", (customer_id,))
        current = cursor.fetchone()
        if current:
            merchant_id, agent_id, version = current["merchant_id"], current["agent_id"], current["mandate_version"] + 1
            cursor.execute("UPDATE mandate_revisions SET active = FALSE WHERE mandate_id = %s OR mandate_id IN (SELECT id FROM mandates WHERE customer_id = %s)", (current["id"], customer_id))
            cursor.execute("INSERT INTO mandate_revisions (mandate_id, version, active) VALUES (%s, %s, FALSE) ON CONFLICT (mandate_id) DO UPDATE SET active = FALSE", (current["id"], current["mandate_version"]))
        else:
            merchant_id, agent_id, version = os.environ.get("MERCHANT_ID"), "agent_demo", 1
        mandate_id = f"mandate_{uuid4().hex}"
        token = mandate_token(customer_id=customer_id, merchant_id=merchant_id, agent_id=agent_id,
                              max_amount_paise=max_amount_paise, allowed_categories=allowed_categories,
                              expires_at=expires_at)
        cursor.execute(
            "INSERT INTO mandates (id, customer_id, merchant_id, agent_id, max_amount_paise, allowed_categories_json, expires_at, token) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (mandate_id, customer_id, merchant_id, agent_id, max_amount_paise, Jsonb(allowed_categories), expires_at, token),
        )
        cursor.execute("INSERT INTO mandate_revisions (mandate_id, version, active) VALUES (%s, %s, TRUE)", (mandate_id, version))
        cursor.execute("INSERT INTO mandate_change_requests (request_id, customer_id, mandate_id) VALUES (%s, %s, %s)", (request_id, customer_id, mandate_id))
        _append_preintent_event(cursor, "MANDATE_UPDATED", {"mandate_id": mandate_id, "max_amount_paise": max_amount_paise})
        cursor.execute("SELECT * FROM mandates WHERE id = %s", (mandate_id,))
        return _mandate_result(cursor.fetchone())


def increase_mandate(customer_id: str, mandate_id: str, cart_id: str, *, request_id: str) -> dict:
    """Raise the current mandate to the server-rounded cart total after a customer click."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"mandate:{customer_id}",))
        cursor.execute("SELECT mandate_id FROM mandate_change_requests WHERE request_id = %s AND customer_id = %s", (request_id, customer_id))
        prior = cursor.fetchone()
        if prior:
            cursor.execute("SELECT * FROM mandates WHERE id = %s", (prior["mandate_id"],))
            return _mandate_result(cursor.fetchone())
        cursor.execute("SELECT m.*, COALESCE(mr.version, 1) AS mandate_version FROM mandates m LEFT JOIN mandate_revisions mr ON mr.mandate_id = m.id WHERE m.id = %s AND m.customer_id = %s AND (mr.mandate_id IS NULL OR mr.active = TRUE) FOR UPDATE OF m", (mandate_id, customer_id))
        current = cursor.fetchone()
        cursor.execute("SELECT c.total_paise FROM carts c WHERE c.id = %s AND c.customer_id = %s", (cart_id, customer_id))
        cart = cursor.fetchone()
        if not current or not cart:
            raise ValueError("Mandate or cart not found")
        cursor.execute("SELECT items_json FROM carts WHERE id = %s", (cart_id,))
        items = cursor.fetchone()["items_json"]
        quantities = {}
        for item in items:
            quantities[item["product_id"]] = quantities.get(item["product_id"], 0) + item["quantity"]
        cursor.execute("SELECT id, price_paise FROM products WHERE id = ANY(%s)", (list(quantities),))
        prices = {row["id"]: row["price_paise"] for row in cursor.fetchall()}
        if set(prices) != set(quantities) or sum(prices[product_id] * quantity for product_id, quantity in quantities.items()) != cart["total_paise"]:
            raise ValueError("Cart total is stale; recreate the cart")
        suggested = ((max(cart["total_paise"], current["max_amount_paise"]) + MANDATE_ROUNDING_PAISE - 1) // MANDATE_ROUNDING_PAISE) * MANDATE_ROUNDING_PAISE
        if suggested <= current["max_amount_paise"]:
            return _mandate_result(current)
        cursor.execute("UPDATE mandate_revisions SET active = FALSE WHERE mandate_id IN (SELECT id FROM mandates WHERE customer_id = %s)", (customer_id,))
        cursor.execute("INSERT INTO mandate_revisions (mandate_id, version, active) VALUES (%s, %s, FALSE) ON CONFLICT (mandate_id) DO UPDATE SET active = FALSE", (current["id"], current["mandate_version"]))
        new_id = f"mandate_{uuid4().hex}"
        token = mandate_token(customer_id=customer_id, merchant_id=current["merchant_id"], agent_id=current["agent_id"],
                              max_amount_paise=suggested, allowed_categories=current["allowed_categories_json"],
                              expires_at=current["expires_at"])
        cursor.execute(
            "INSERT INTO mandates (id, customer_id, merchant_id, agent_id, max_amount_paise, allowed_categories_json, expires_at, token) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (new_id, customer_id, current["merchant_id"], current["agent_id"], suggested,
             Jsonb(current["allowed_categories_json"]), current["expires_at"], token),
        )
        cursor.execute("INSERT INTO mandate_revisions (mandate_id, version, active) VALUES (%s, %s, TRUE)", (new_id, current["mandate_version"] + 1))
        cursor.execute("INSERT INTO mandate_change_requests (request_id, customer_id, mandate_id) VALUES (%s, %s, %s)", (request_id, customer_id, new_id))
        _append_preintent_event(cursor, "MANDATE_INCREASED", {"mandate_id": new_id, "max_amount_paise": suggested, "cart_id": cart_id})
        cursor.execute("SELECT * FROM mandates WHERE id = %s", (new_id,))
        return _mandate_result(cursor.fetchone())


def _append_preintent_event(cursor, event_type: str, payload: dict) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('audit:preintent', 0))")
    cursor.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM audit_events WHERE intent_id IS NULL")
    sequence = cursor.fetchone()["next_sequence"]
    cursor.execute(
        "INSERT INTO audit_events (id, intent_id, attempt_id, sequence, type, actor, evidence_source, payload_json) "
        "VALUES (%s, NULL, NULL, %s, %s, 'CUSTOMER', 'CUSTOMER', %s)",
        (f"audit_{uuid4().hex}", sequence, event_type, Jsonb(payload)),
    )


def mandate_token(
    *,
    customer_id: str,
    merchant_id: str,
    agent_id: str,
    max_amount_paise: int,
    allowed_categories: list[str],
    expires_at: datetime,
) -> str:
    """Calculate the HMAC token that binds mandate fields to the configured merchant."""
    secret = os.environ.get("MANDATE_SIGNING_SECRET")
    if not secret:
        raise RuntimeError("MANDATE_SIGNING_SECRET must be set")
    payload = json.dumps(
        {
            "agent_id": agent_id,
            "allowed_categories": sorted(allowed_categories),
            "customer_id": customer_id,
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "max_amount_paise": max_amount_paise,
            "merchant_id": merchant_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def validate_purchase(
    cart_id: str, mandate_id: str, *, customer_id: str | None = None
) -> dict:
    """Validate a cart against a signed mandate and append the decision audit record."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        return _validate_purchase(cursor, cart_id, mandate_id, customer_id)


def _validate_purchase(
    cursor, cart_id: str, mandate_id: str, customer_id: str | None = None
) -> dict:
    """Run validation in the caller's transaction so checkout cannot bypass it."""
    reasons: list[str] = []
    cursor.execute("SELECT * FROM carts WHERE id = %s", (cart_id,))
    cart = cursor.fetchone()
    cursor.execute("SELECT * FROM mandates WHERE id = %s", (mandate_id,))
    mandate = cursor.fetchone()

    if cart is None:
        reasons.append("Cart does not exist")
    if mandate is None:
        reasons.append("Mandate does not exist")
    if cart and mandate:
        if customer_id is not None and cart["customer_id"] != customer_id:
            reasons.append("Cart does not belong to this customer")
        _validate_cart_and_mandate(cursor, cart, mandate, reasons)

    result = {
        "allowed": not reasons,
        "reasons": reasons,
        "cart_id": cart_id,
        "mandate_id": mandate_id,
        "cart_total_paise": cart["total_paise"] if cart else None,
        "mandate_cap_paise": mandate["max_amount_paise"] if mandate else None,
    }
    _append_validation_audit(cursor, result)
    return result


def _validate_cart_and_mandate(cursor, cart: dict, mandate: dict, reasons: list[str]) -> None:
    if cart["customer_id"] != mandate["customer_id"]:
        reasons.append("Cart customer does not match mandate customer")
    if mandate["merchant_id"] != os.environ.get("MERCHANT_ID"):
        reasons.append("Mandate merchant does not match this merchant")
    if mandate["expires_at"] <= datetime.now(timezone.utc):
        reasons.append("Mandate has expired")
    cursor.execute("SELECT active FROM mandate_revisions WHERE mandate_id = %s", (mandate["id"],))
    revision = cursor.fetchone()
    if revision and not revision["active"]:
        reasons.append("Mandate is no longer current")
    try:
        expected_token = mandate_token(
            customer_id=mandate["customer_id"],
            merchant_id=mandate["merchant_id"],
            agent_id=mandate["agent_id"],
            max_amount_paise=mandate["max_amount_paise"],
            allowed_categories=mandate["allowed_categories_json"],
            expires_at=mandate["expires_at"],
        )
        if not hmac.compare_digest(mandate["token"], expected_token):
            reasons.append("Mandate signature is invalid")
    except (RuntimeError, TypeError):
        reasons.append("Mandate signature cannot be verified")

    items = cart["items_json"]
    if not isinstance(items, list):
        reasons.append("Cart items are invalid")
        return
    quantities = {item.get("product_id"): item.get("quantity") for item in items}
    if not quantities or any(
        not isinstance(product_id, str)
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity <= 0
        for product_id, quantity in quantities.items()
    ):
        reasons.append("Cart items are invalid")
        return
    if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
        reasons.append(f"Quantity cannot exceed {MAX_ITEM_QUANTITY}")
        return

    cursor.execute(
        "SELECT id, category, price_paise, stock, restricted FROM products WHERE id = ANY(%s)",
        (list(quantities),),
    )
    products = {product["id"]: product for product in cursor.fetchall()}
    allowed_categories = set(mandate["allowed_categories_json"])
    for product_id, quantity in quantities.items():
        product = products.get(product_id)
        if product is None:
            reasons.append(f"Product {product_id} no longer exists")
            continue
        if quantity > product["stock"]:
            reasons.append(f"Insufficient stock for {product_id}")
        if product["restricted"]:
            reasons.append(f"Product {product_id} is restricted")
        if product["category"] not in allowed_categories:
            reasons.append(f"Product {product_id} category is not allowed")

    current_total = sum(
        products[product_id]["price_paise"] * quantity
        for product_id, quantity in quantities.items()
        if product_id in products
    )
    if current_total != cart["total_paise"]:
        reasons.append("Cart total is stale; recreate the cart")
    if cart["total_paise"] < 0 or cart["total_paise"] > MAX_CART_TOTAL_PAISE:
        reasons.append("Cart total is outside the supported range")
    if current_total > mandate["max_amount_paise"]:
        reasons.append(
            f"Cart total {current_total} paise exceeds mandate cap "
            f"{mandate['max_amount_paise']} paise"
        )


def _append_validation_audit(cursor, result: dict) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('audit:preintent', 0))")
    cursor.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
        "FROM audit_events WHERE intent_id IS NULL"
    )
    sequence = cursor.fetchone()["next_sequence"]
    cursor.execute(
        "INSERT INTO audit_events "
        "(id, intent_id, attempt_id, sequence, type, actor, evidence_source, payload_json) "
        "VALUES (%s, NULL, NULL, %s, 'MANDATE_VALIDATION', 'SERVER', "
        "'MANDATE_VALIDATION', %s)",
        (f"audit_{uuid4().hex}", sequence, Jsonb(result)),
    )
