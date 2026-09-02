import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

from agent.tools import TOOL_DEFINITIONS, tools_for
from agent.loop import run_agent
from backend.catalogue import create_cart, get_product_details, mandate_token, search_catalogue, update_mandate, validate_purchase
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
            cursor.execute(
                "INSERT INTO products VALUES "
                "('product_agent', 'Book', 'Book', 'books', 40000, 1, FALSE), "
                "('product_shirt', 'T-Shirt', 'Cotton shirt', 'tshirts', 30000, 1, FALSE), "
                "('product_pants', 'Pants', 'Cotton pants', 'pants', 30000, 1, FALSE)"
            )
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

        self.tools.record_customer_message("Payment is being confirmed.", checkout["intent_id"])
        self.assertEqual(self.tools.get_audit_timeline(checkout["intent_id"])[-1]["type"], "CUSTOMER_MESSAGE")

    def test_customer_scoping_blocks_other_customers_cart(self) -> None:
        cart = create_cart([{"product_id": "product_agent", "quantity": 1}], customer_id="other_customer")
        result = self.tools.validate_purchase(cart["cart_id"], "mandate_agent")
        self.assertFalse(result["allowed"])
        self.assertIn("Cart does not belong to this customer", result["reasons"])

    def test_out_of_category_product_is_allowed_within_spending_mandate(self) -> None:
        cart = self.tools.create_cart([{"product_id": "product_pants", "quantity": 1}])
        self.assertTrue(self.tools.validate_purchase(cart["cart_id"], "mandate_agent")["allowed"])

    def test_search_and_details_are_mandate_scoped(self) -> None:
        self.assertEqual([item["category"] for item in self.tools.search_catalogue("Book")], ["books"])
        self.assertEqual([item["category"] for item in self.tools.search_catalogue("books")], ["books"])
        self.assertEqual([item["category"] for item in self.tools.search_catalogue("books", category="books")], ["books"])
        self.assertEqual(self.tools.search_catalogue("Cotton", category="tshirts"), [])
        self.assertIsNone(self.tools.get_product_details("product_shirt"))
        self.assertEqual(get_product_details("product_shirt")["category"], "tshirts")
        self.assertEqual({item["category"] for item in self.tools.search_catalogue("tshirt", category="tshirts")}, set())

    def test_natural_tshirt_search_forms_use_the_allowed_category(self) -> None:
        from backend.catalogue import update_mandate
        update_mandate(
            "customer_agent", 100000, ["tshirts"],
            datetime.now(timezone.utc) + timedelta(days=2), request_id="tshirt-search-mandate",
        )
        expected = {"product_shirt"}
        for query in ("tshirt", "tshirts", "T-Shirt", "T-Shirts", "t-shirts"):
            with self.subTest(query=query):
                self.assertEqual({item["id"] for item in self.tools.search_catalogue(query)}, expected)
        self.assertEqual({item["id"] for item in self.tools.search_catalogue("T-Shirts", category="T-Shirts")}, expected)

    def test_agent_cannot_mutate_mandate(self) -> None:
        self.assertFalse(hasattr(self.tools, "update_mandate"))
        self.assertFalse(hasattr(self.tools, "increase_mandate"))

    def test_mixed_request_returns_products_before_mandate_validation(self) -> None:
        update_mandate(
            "customer_agent", 100000, ["tshirts", "pants"],
            datetime.now(timezone.utc) + timedelta(days=2), request_id="mixed-mandate",
        )

        seen_requests = []

        def model(context):
            seen_requests.append(context["request"])
            if len(context["history"]) == 0:
                return {"tool": "get_mandate", "arguments": {}}
            if len(context["history"]) == 1:
                return {"tool": "search_catalogue", "arguments": {"query": "tshirts"}}
            if len(context["history"]) == 2:
                return {"tool": "search_catalogue", "arguments": {"query": "Book", "category": "books"}}
            return {"tool": "respond_to_customer", "arguments": {"message": "I found T-Shirt options. Books are outside your current mandate, so I left them out."}}

        result = run_agent("I want 2 tshirts and 3 books", customer_id="customer_agent", model=model)
        searches = [entry["result"] for entry in result["history"] if entry.get("tool") == "search_catalogue"]
        self.assertEqual({product["category"] for products in searches for product in products}, {"tshirts"})
        self.assertEqual([product["id"] for product in searches[0]], ["product_shirt"])
        self.assertIn("Books", result["message"])
        self.assertIn("outside", result["message"])
        self.assertTrue(all(request == "I want 2 tshirts and 3 books" for request in seen_requests))
