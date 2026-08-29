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


def search_catalogue(query: str, category: str | None = None) -> list[dict]:
    """Return product summaries matching a customer query."""
    filters = ["(name ILIKE %s OR description ILIKE %s)"]
    values: list[str] = [f"%{query}%", f"%{query}%"]
    if category:
        filters.append("category = %s")
        values.append(category)
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT id, name, description, category, price_paise, stock, restricted "
            "FROM products WHERE " + " AND ".join(filters) + " ORDER BY name",
            values,
        )
        return list(cursor.fetchall())


def get_product_details(product_id: str) -> dict | None:
    """Return the current server-authoritative product record."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT * FROM products WHERE id = %s", (product_id,))
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
            "allowed_categories_json, expires_at FROM mandates "
            "WHERE customer_id = %s ORDER BY expires_at DESC LIMIT 1",
            (customer_id,),
        )
        return cursor.fetchone()


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
    if current_total > mandate["max_amount_paise"]:
        reasons.append(
            f"Cart total {current_total} paise exceeds mandate cap "
            f"{mandate['max_amount_paise']} paise"
        )


def _append_validation_audit(cursor, result: dict) -> None:
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
