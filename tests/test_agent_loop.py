import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from psycopg.types.json import Jsonb

from agent.loop import run_agent
from backend.catalogue import mandate_token
from backend.db import connect, migrate


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class AgentLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ.update(MANDATE_SIGNING_SECRET="test-secret", MERCHANT_ID="merchant_demo")

    def setUp(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id="customer_loop", merchant_id="merchant_demo", agent_id="agent_1",
            max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
            cursor.execute("INSERT INTO products VALUES ('product_loop', 'Book', 'Book', 'books', 40000, 1, FALSE)")
            cursor.execute("INSERT INTO mandates VALUES ('mandate_loop', 'customer_loop', 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (Jsonb(["books"]), expires_at, token))

    @patch("agent.tools.start_checkout", return_value={"allowed": True, "intent_id": "intent_loop", "order_id": "order_loop", "status": "PENDING"})
    def test_model_selects_tools_and_cannot_start_a_second_pending_checkout(self, start_checkout) -> None:
        def model(context: dict) -> dict:
            history = context["history"]
            if not history:
                return {"tool": "search_catalogue", "arguments": {"query": "book"}}
            if history[-1].get("error"):
                return {"tool": "get_payment_status", "arguments": {"intent_id": "intent_loop"}}
            if history[-1].get("tool") == "search_catalogue":
                return {"tool": "create_cart", "arguments": {"items": [{"product_id": "product_loop", "quantity": 1}]}}
            if history[-1].get("tool") == "create_cart":
                return {"tool": "get_mandate", "arguments": {}}
            if history[-1].get("tool") == "get_mandate":
                return {"tool": "validate_purchase", "arguments": {"cart_id": history[-2]["result"]["cart_id"], "mandate_id": "mandate_loop"}}
            if history[-1].get("tool") == "validate_purchase":
                return {"tool": "start_checkout", "arguments": {"cart_id": history[-3]["result"]["cart_id"], "mandate_id": "mandate_loop", "client_request_id": "loop_request"}}
            if history[-1].get("tool") == "start_checkout":
                return {"tool": "start_checkout", "arguments": {"cart_id": "ignored", "mandate_id": "ignored", "client_request_id": "second_request"}}
            return {"tool": "respond_to_customer", "arguments": {"message": "Payment is being confirmed. I will not start another payment."}}

        result = run_agent("Buy a book", customer_id="customer_loop", model=model)
        self.assertEqual(result["message"], "Payment is being confirmed. I will not start another payment.")
        self.assertEqual(start_checkout.call_count, 1)
        self.assertIn("get_payment_status", [entry.get("tool") for entry in result["history"]])

    @patch("agent.loop.tools_for")
    def test_explicit_supported_quantities_override_model_default_for_searched_products(self, tools_for) -> None:
        tools = Mock()
        tools.search_catalogue.return_value = [
            {"id": "shirt", "category": "tshirts"}, {"id": "pants", "category": "pants"},
        ]
        tools.create_cart.return_value = {"cart_id": "cart_1", "total_paise": 1}
        tools.get_mandate.return_value = {"id": "mandate_1"}
        tools.validate_purchase.return_value = {"allowed": True}
        tools.start_checkout.return_value = {"allowed": True, "status": "PENDING", "intent_id": "intent_1"}
        tools_for.return_value = tools
        actions = iter([
            {"tool": "search_catalogue", "arguments": {"query": "T-Shirts"}},
            {"tool": "create_cart", "arguments": {"items": [{"product_id": "shirt", "quantity": 1}, {"product_id": "pants", "quantity": 1}]}},
            {"tool": "get_mandate", "arguments": {}},
            {"tool": "validate_purchase", "arguments": {"cart_id": "cart_1", "mandate_id": "mandate_1"}},
            {"tool": "start_checkout", "arguments": {"cart_id": "cart_1", "mandate_id": "mandate_1", "client_request_id": "request_1"}},
            {"final": "Checkout is ready."},
        ])
        run_agent("2 T-Shirts and 3 pants", customer_id="customer_demo", model=lambda _context: next(actions))
        self.assertEqual(tools.create_cart.call_args.kwargs["items"], [
            {"product_id": "shirt", "quantity": 2}, {"product_id": "pants", "quantity": 3},
        ])
