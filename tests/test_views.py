import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

from backend.catalogue import create_cart, mandate_token
from backend.checkout import start_checkout
from backend.db import connect, migrate
from backend.views import customer_intent, customer_transactions, operator_intent


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ.update(MANDATE_SIGNING_SECRET="test-secret", MERCHANT_ID="merchant_demo")

    def setUp(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id="customer_view", merchant_id="merchant_demo", agent_id="agent_1",
            max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
            cursor.execute("INSERT INTO products VALUES ('product_view', 'Book', 'Book', 'books', 40000, 1, FALSE)")
            cursor.execute("INSERT INTO mandates VALUES ('mandate_view', 'customer_view', 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (Jsonb(["books"]), expires_at, token))
        self.cart = create_cart([{"product_id": "product_view", "quantity": 1}], customer_id="customer_view")

    @patch("backend.checkout.create_order", return_value="order_view")
    def test_customer_and_operator_views_are_safe_and_read_only(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_view", "request_view", customer_id="customer_view")
        customer = customer_intent(checkout["intent_id"])
        self.assertEqual(customer["message"], "Payment is being confirmed. Do not retry.")
        self.assertEqual(customer["checkout"]["order_id"], "order_view")
        history = customer_transactions("customer_view")
        self.assertEqual(history["transactions"][0]["intent_id"], checkout["intent_id"])
        self.assertEqual(history["transactions"][0]["product"], "Book")
        operator = operator_intent(checkout["intent_id"])
        self.assertEqual((operator["attempt_id"].startswith("attempt_"), operator["status"]), (True, "PENDING"))
        self.assertEqual(operator["timeline"][-1]["type"], "RAZORPAY_ORDER_CREATED")
        self.assertNotIn("razorpay_order_id", operator["timeline"][-1]["detail"])

    @patch("backend.checkout.create_order", return_value="order_view_owner")
    def test_customer_projection_rejects_other_customer(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_view", "request_owner", customer_id="customer_view")
        self.assertFalse(customer_intent(checkout["intent_id"], "customer_other")["found"])
