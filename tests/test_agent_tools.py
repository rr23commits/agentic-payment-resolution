import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

from agent.tools import TOOL_DEFINITIONS, tools_for
from backend.catalogue import create_cart, mandate_token
from backend.db import connect, migrate


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class AgentToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ.update(MANDATE_SIGNING_SECRET="test-secret", MERCHANT_ID="merchant_demo")

    def setUp(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id="customer_agent", merchant_id="merchant_demo", agent_id="agent_1",
            max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
            cursor.execute("INSERT INTO products VALUES ('product_agent', 'Book', 'Book', 'books', 40000, 1, FALSE)")
            cursor.execute("INSERT INTO mandates VALUES ('mandate_agent', 'customer_agent', 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (Jsonb(["books"]), expires_at, token))
        self.tools = tools_for("customer_agent")

    @patch("backend.checkout.create_order", return_value="order_agent")
    def test_allowlist_scopes_checkout_status_and_audit(self, _create_order) -> None:
        self.assertEqual([tool["name"] for tool in TOOL_DEFINITIONS], [
            "respond_to_customer",
            "search_catalogue", "get_product_details", "create_cart", "get_mandate",
            "validate_purchase", "start_checkout", "get_payment_status", "get_audit_timeline",
        ])
        cart = self.tools.create_cart([{"product_id": "product_agent", "quantity": 1}])
        self.assertTrue(self.tools.validate_purchase(cart["cart_id"], "mandate_agent")["allowed"])
        checkout = self.tools.start_checkout(cart["cart_id"], "mandate_agent", "agent_request")
        status = self.tools.get_payment_status(checkout["intent_id"])
        self.assertEqual((status["status"], status["message"]), ("PENDING", "Payment is being confirmed. Do not retry."))
        self.assertEqual(self.tools.get_audit_timeline(checkout["intent_id"])[-1]["type"], "RAZORPAY_ORDER_CREATED")
        self.assertFalse(tools_for("another_customer").get_payment_status(checkout["intent_id"])["found"])

    def test_customer_scoping_blocks_other_customers_cart(self) -> None:
        cart = create_cart([{"product_id": "product_agent", "quantity": 1}], customer_id="other_customer")
        result = self.tools.validate_purchase(cart["cart_id"], "mandate_agent")
        self.assertFalse(result["allowed"])
        self.assertIn("Cart does not belong to this customer", result["reasons"])
