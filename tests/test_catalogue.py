import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from backend.catalogue import (
    create_cart,
    get_mandate,
    get_product_details,
    mandate_token,
    search_catalogue,
    validate_purchase,
)
from backend.db import connect, migrate


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class CatalogueToolTests(unittest.TestCase):
    customer_id = "customer_1"

    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ["MANDATE_SIGNING_SECRET"] = "test-secret"
        os.environ["MERCHANT_ID"] = "merchant_demo"

    def setUp(self) -> None:
        with connect() as connection, connection.cursor() as cursor:
            # Test-only reset: production audit rows are never truncated by app code.
            cursor.execute(
                "TRUNCATE audit_events, payment_attempts, checkout_intents, carts, mandates, "
                "products CASCADE"
            )
            cursor.execute(
                "INSERT INTO products VALUES "
                "('product_book', 'Book', 'A useful book', 'books', 40000, 3, FALSE), "
                "('product_game', 'Game', 'A game', 'games', 20000, 3, FALSE), "
                "('product_locked', 'Locked', 'Restricted', 'books', 10000, 3, TRUE)"
            )

    def add_mandate(self, *, cap: int = 50000, categories: list[str] | None = None,
                    expires_at: datetime | None = None, token: str | None = None,
                    merchant_id: str = "merchant_demo") -> str:
        categories = categories or ["books"]
        expires_at = expires_at or datetime.now(timezone.utc) + timedelta(days=1)
        token = token or mandate_token(
            customer_id=self.customer_id,
            merchant_id=merchant_id,
            agent_id="agent_1",
            max_amount_paise=cap,
            allowed_categories=categories,
            expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO mandates VALUES (%s, %s, %s, 'agent_1', %s, %s, %s, %s)",
                ("mandate_1", self.customer_id, merchant_id, cap, Jsonb(categories), expires_at, token),
            )
        return "mandate_1"

    def test_cart_uses_server_price_and_validates_signed_mandate(self) -> None:
        mandate_id = self.add_mandate()
        cart = create_cart(
            [{"product_id": "product_book", "quantity": 1, "price_paise": 1}],
            customer_id=self.customer_id,
        )

        self.assertEqual(cart["total_paise"], 40000)
        self.assertTrue(validate_purchase(cart["cart_id"], mandate_id)["allowed"])
        self.assertEqual(get_mandate(self.customer_id)["id"], mandate_id)
        products = search_catalogue("book", "books")
        self.assertEqual(len(products), 1)
        self.assertNotIn("stock", products[0])
        self.assertNotIn("restricted", products[0])
        self.assertEqual(get_product_details("product_book")["price_paise"], 40000)
        self.assertEqual(search_catalogue("Locked"), [])
        self.assertIsNone(get_product_details("product_locked"))

    def test_cap_category_expiry_and_signature_failures_are_audited(self) -> None:
        cart = create_cart(
            [{"product_id": "product_book", "quantity": 1}], customer_id=self.customer_id
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM audit_events")
            audit_count = cursor.fetchone()[0]
        cases = [
            {"cap": 1},
            {"categories": ["games"]},
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            {"token": "not-a-signature"},
        ]
        for case in cases:
            with connect() as connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM mandates")
            mandate_id = self.add_mandate(**case)
            self.assertFalse(validate_purchase(cart["cart_id"], mandate_id)["allowed"])

        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM audit_events")
            self.assertEqual(cursor.fetchone()[0], audit_count + len(cases))

    def test_concurrent_preintent_audits_have_unique_sequences(self) -> None:
        mandate_id = self.add_mandate()
        cart = create_cart([{"product_id": "product_book", "quantity": 1}], customer_id=self.customer_id)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _item: validate_purchase(cart["cart_id"], mandate_id), range(8)))
        self.assertTrue(all(result["allowed"] for result in results))
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT sequence, COUNT(*) FROM audit_events WHERE intent_id IS NULL GROUP BY sequence HAVING COUNT(*) > 1")
            self.assertEqual(cursor.fetchall(), [])

    def test_stock_failure_cannot_create_a_cart(self) -> None:
        with self.assertRaisesRegex(ValueError, "Insufficient stock"):
            create_cart(
                [{"product_id": "product_book", "quantity": 4}],
                customer_id=self.customer_id,
            )

    def test_restricted_product_and_wrong_merchant_are_rejected(self) -> None:
        restricted_cart = create_cart(
            [{"product_id": "product_locked", "quantity": 1}],
            customer_id=self.customer_id,
        )
        self.assertFalse(
            validate_purchase(restricted_cart["cart_id"], self.add_mandate())["allowed"]
        )

        cart = create_cart(
            [{"product_id": "product_book", "quantity": 1}], customer_id=self.customer_id
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM mandates")
        result = validate_purchase(
            cart["cart_id"], self.add_mandate(merchant_id="merchant_other")
        )
        self.assertFalse(result["allowed"])
        self.assertIn("Mandate merchant does not match this merchant", result["reasons"])
