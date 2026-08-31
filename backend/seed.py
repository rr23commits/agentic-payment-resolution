"""Idempotent local demo catalogue and mandates."""

import os
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from backend.catalogue import mandate_token
from backend.db import connect


def main() -> None:
    merchant_id = os.environ.get("MERCHANT_ID", "merchant_demo")
    customer_id = "customer_demo"
    now = datetime.now(timezone.utc)
    products = [
        ("product_demo_book", "The Demo Book", "A purchasable demo book", "books", 40000, 10, False),
        ("product_demo_game", "The Demo Game", "A category-failure demo product", "games", 2500, 10, False),
        ("product_demo_tshirt_blue", "Blue Cotton T-Shirt", "Soft blue everyday t-shirt", "tshirts", 49900, 10, False),
        ("product_demo_tshirt_black", "Black Cotton T-Shirt", "Classic black everyday t-shirt", "tshirts", 59900, 10, False),
        ("product_demo_pants_chino", "Khaki Chinos", "Relaxed fit khaki pants", "pants", 79900, 10, False),
        ("product_demo_pants_denim", "Indigo Denim Pants", "Straight fit denim pants", "pants", 89900, 10, False),
    ]
    mandates = [
        ("mandate_demo_valid", 50000, ["books"], now + timedelta(days=30)),
        ("mandate_demo_cap", 100, ["books"], now + timedelta(days=29)),
        ("mandate_demo_category", 50000, ["games"], now + timedelta(days=29)),
        ("mandate_demo_expired", 50000, ["books"], now - timedelta(days=1)),
    ]
    with connect() as connection, connection.cursor() as cursor:
        # Keep test-only product fixtures out of the normal demo catalogue.
        cursor.execute("DELETE FROM products WHERE id IN ('product_loop', 'product_webhook')")
        for product in products:
            cursor.execute(
                "INSERT INTO products (id, name, description, category, price_paise, stock, restricted) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, description = EXCLUDED.description, category = EXCLUDED.category, "
                "price_paise = EXCLUDED.price_paise, stock = EXCLUDED.stock, restricted = EXCLUDED.restricted",
                product,
            )
        for mandate_id, cap, categories, expires_at in mandates:
            token = mandate_token(
                customer_id=customer_id, merchant_id=merchant_id, agent_id="agent_demo",
                max_amount_paise=cap, allowed_categories=categories, expires_at=expires_at,
            )
            cursor.execute(
                "INSERT INTO mandates (id, customer_id, merchant_id, agent_id, max_amount_paise, "
                "allowed_categories_json, expires_at, token) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET merchant_id = EXCLUDED.merchant_id, "
                "max_amount_paise = EXCLUDED.max_amount_paise, allowed_categories_json = EXCLUDED.allowed_categories_json, "
                "expires_at = EXCLUDED.expires_at, token = EXCLUDED.token",
                (mandate_id, customer_id, merchant_id, "agent_demo", cap, Jsonb(categories), expires_at, token),
            )
    print("Demo products and mandates are ready.")


if __name__ == "__main__":
    main()
