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
    # Match the natural-language singular form too, so plural requests find singular catalogue names.
    terms = [query, query[:-1]] if query.casefold().endswith("s") and len(query) > 1 else [query]
    filters = ["(" + " OR ".join("name ILIKE %s OR description ILIKE %s" for _ in terms) + ")"]
    values: list[str] = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
    if category:
        filters.append("category = %s")
        values.append(category)
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT p.id, p.name, p.description, p.category, p.price_paise, "
            "COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, "
            "pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise, "
            "COALESCE(pm.related_product_ids_json, '[]'::jsonb) AS related_product_ids, pm.recommendation_reason "
            "FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id "
            "WHERE p.restricted = FALSE AND " + " AND ".join(filters) + " ORDER BY p.name LIMIT %s",
            [*values, limit],
        )
        return _mark_products(_attach_recommendations(cursor, list(cursor.fetchall())))


def search_catalogue_for_customer(customer_id: str, query: str, category: str | None = None, limit: int = 4) -> list[dict]:
    """Search only categories in the customer's current mandate."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
        raise ValueError("Search limit must be between 1 and 4")
    mandate = get_mandate(customer_id)
    allowed = set(mandate["allowed_categories_json"]) if mandate and mandate["expires_at"] > datetime.now(timezone.utc) else set()
    if category and category.casefold().replace("-", "").replace(" ", "") in {"tshirt", "tshirts"}:
        category = "tshirts"
    if category and category not in allowed:
        return []
    if query.casefold().replace("-", "").replace(" ", "") in {"tshirt", "tshirts"}:
        query = "shirt"
    terms = [query, query[:-1]] if query.casefold().endswith("s") and len(query) > 1 else [query]
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        filters = ["(" + " OR ".join("name ILIKE %s OR description ILIKE %s" for _ in terms) + ")", "category = ANY(%s)"]
        values: list[object] = [value for term in terms for value in (f"%{term}%", f"%{term}%")] + [list(allowed)]
        if category:
            filters.append("category = %s")
            values.append(category)
        cursor.execute(
            "SELECT p.id, p.name, p.description, p.category, p.price_paise, "
            "COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, "
            "pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise, "
            "COALESCE(pm.related_product_ids_json, '[]'::jsonb) AS related_product_ids, pm.recommendation_reason "
            "FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id "
            "WHERE p.restricted = FALSE AND " + " AND ".join(filters) + " ORDER BY p.name LIMIT %s",
            [*values, limit],
        )
        return _mark_products(_attach_recommendations(cursor, list(cursor.fetchall())))


def get_product_details(product_id: str) -> dict | None:
    """Return the current server-authoritative product record."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT p.id, p.name, p.description, p.category, p.price_paise, p.stock, COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise, COALESCE(pm.related_product_ids_json, '[]'::jsonb) AS related_product_ids, pm.recommendation_reason FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id WHERE p.id = %s AND p.restricted = FALSE", (product_id,))
        row = cursor.fetchone()
        return _mark_products(_attach_recommendations(cursor, [row]))[0] if row else None


def get_product_details_for_customer(customer_id: str, product_id: str) -> dict | None:
    """Return product details only when its category is mandate-authorized."""
    mandate = get_mandate(customer_id)
    allowed = set(mandate["allowed_categories_json"]) if mandate and mandate["expires_at"] > datetime.now(timezone.utc) else set()
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT p.id, p.name, p.description, p.category, p.price_paise, p.stock, COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise, COALESCE(pm.related_product_ids_json, '[]'::jsonb) AS related_product_ids, pm.recommendation_reason FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id "
            "WHERE p.id = %s AND p.restricted = FALSE AND p.category = ANY(%s)",
            (product_id, list(allowed)),
        )
        row = cursor.fetchone()
        return _mark_products(_attach_recommendations(cursor, [row]))[0] if row else None


def _attach_recommendations(cursor, products: list[dict]) -> list[dict]:
    ids = {related for product in products for related in product.get("related_product_ids", [])}
    if not ids:
        return [{**product, "recommendations": []} for product in products]
    cursor.execute("SELECT p.id, p.name, p.category, p.price_paise, COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id WHERE p.id = ANY(%s) AND p.restricted = FALSE", (list(ids),))
    related = {row["id"]: row for row in cursor.fetchall()}
    return [{**product, "recommendations": [{**_offer_fields(related[item], eligible_categories={product.get("category")}), "source": "recommendation", "reason": product.get("recommendation_reason") or "Complements this item."} for item in product.get("related_product_ids", []) if item in related]} for product in products]


def _mark_products(products: list[dict]) -> list[dict]:
    return [{**_offer_fields(product), "source": "search"} for product in products]


def _offer_fields(product: dict, *, eligible_categories: set[str] | None = None) -> dict:
    """Expose only valid demo offer metadata; the product price remains payable authority."""
    eligibility = product.get("offer_eligibility")
    valid_until = product.get("offer_valid_until")
    eligible = eligibility == "All customers" or (eligibility == "With a book" and "books" in (eligible_categories or set()))
    active = bool(eligible and valid_until and valid_until >= datetime.now(timezone.utc).date() and product.get("list_price_paise", product["price_paise"]) > product["price_paise"] and product.get("savings_paise", 0) == product["list_price_paise"] - product["price_paise"])
    return {**product, "payable_price_paise": product["price_paise"], "offer_active": active, "savings_paise": product.get("savings_paise", 0) if active else 0}


def merchant_catalogue() -> dict:
    """Return safe, read-only merchant catalogue data for AI buyers."""
    with connect() as connection, connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT p.id, p.name, p.description, p.category, p.price_paise, p.stock, p.restricted, COALESCE(pm.list_price_paise, p.price_paise) AS list_price_paise, pm.offer_label, pm.offer_eligibility, pm.offer_valid_until, COALESCE(pm.savings_paise, 0) AS savings_paise, COALESCE(pm.related_product_ids_json, '[]'::jsonb) AS related_product_ids, pm.recommendation_reason FROM products p LEFT JOIN product_metadata pm ON pm.product_id = p.id ORDER BY p.category, p.name")
        rows = list(cursor.fetchall())
    products = [{**_offer_fields({key: value for key, value in row.items() if key != "restricted"}), "eligible": not row["restricted"], "source": "search"} for row in rows]
    by_id = {product["id"]: product for product in products}
    for product in products:
        product["related_products"] = [{"id": related, "name": by_id[related]["name"], "category": by_id[related]["category"], "source": "recommendation", "reason": product.get("recommendation_reason") or "Complements this item."} for related in product.get("related_product_ids", []) if related in by_id]
    return {"merchant": {"id": os.environ.get("MERCHANT_ID", "merchant_demo"), "name": "Demo Merchant"}, "categories": sorted({p["category"] for p in products}), "products": products, "checkout_requirements": {"mandate": "signed customer mandate", "currency": "INR", "payment": "Razorpay Test Mode", "single_cart": True}}


def create_cart(items: list[dict], *, customer_id: str) -> dict:
    """Reload products and create a cart; mandate eligibility is checked later."""
    quantities: dict[str, int] = defaultdict(int)
    for item in items:
        product_id, quantity, source = item.get("product_id"), item.get("quantity"), item.get("source")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError("Each item needs a product_id")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Each item needs a positive integer quantity")
        if source is not None and source not in {"search", "recommendation"}:
            raise ValueError("Item source must be search or recommendation")
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

        sources = {item["product_id"]: item.get("source") for item in items if item.get("source")}
        cart_items = [{"product_id": product_id, "quantity": quantity, **({"source": sources[product_id]} if product_id in sources else {})} for product_id, quantity in quantities.items()]
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
    for product_id, quantity in quantities.items():
        product = products.get(product_id)
        if product is None:
            reasons.append(f"Product {product_id} no longer exists")
            continue
        if quantity > product["stock"]:
            reasons.append(f"Insufficient stock for {product_id}")
        if product["restricted"]:
            reasons.append(f"Product {product_id} is restricted")

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
