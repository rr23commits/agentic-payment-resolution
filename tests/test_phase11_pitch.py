import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

from agent.loop import run_agent
from agent.tools import tools_for
from backend.catalogue import mandate_token
from backend.db import connect, migrate
from backend.webhooks import ingest_webhook


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class PitchFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ.update(
            MANDATE_SIGNING_SECRET="test-secret",
            MERCHANT_ID="merchant_demo",
            RAZORPAY_WEBHOOK_SECRET="webhook-secret",
        )

    def setUp(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id="customer_pitch", merchant_id="merchant_demo", agent_id="agent_1",
            max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
            cursor.execute("INSERT INTO products VALUES ('product_pitch', 'Book', 'Book', 'books', 40000, 1, FALSE)")
            cursor.execute("INSERT INTO mandates VALUES ('mandate_pitch', 'customer_pitch', 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (Jsonb(["books"]), expires_at, token))

    @patch("backend.checkout.create_order", return_value="order_pitch")
    def test_one_attempt_is_blocked_then_resolved_with_a_visible_trail(self, create_order) -> None:
        def model(context: dict) -> dict:
            last_tool = context["history"][-1].get("tool") if context["history"] else None
            if context["history"] and context["history"][-1].get("error"):
                return {"tool": "respond_to_customer", "arguments": {"message": "Payment is being confirmed. I will not start another payment."}}
            if last_tool is None:
                return {"tool": "search_catalogue", "arguments": {"query": "book"}}
            if last_tool == "search_catalogue":
                return {"tool": "create_cart", "arguments": {"items": [{"product_id": "product_pitch", "quantity": 1}]}}
            if last_tool == "create_cart":
                return {"tool": "get_mandate", "arguments": {}}
            if last_tool == "get_mandate":
                return {"tool": "validate_purchase", "arguments": {"cart_id": context["history"][-2]["result"]["cart_id"], "mandate_id": "mandate_pitch"}}
            if last_tool == "validate_purchase":
                return {"tool": "start_checkout", "arguments": {"cart_id": context["history"][-3]["result"]["cart_id"], "mandate_id": "mandate_pitch", "client_request_id": "pitch_first"}}
            if last_tool == "start_checkout":
                return {"tool": "start_checkout", "arguments": {"cart_id": "ignored", "mandate_id": "ignored", "client_request_id": "pitch_second"}}
            return {"tool": "respond_to_customer", "arguments": {"message": "Payment is being confirmed. I will not start another payment."}}

        result = run_agent("Buy a book", customer_id="customer_pitch", model=model)
        checkout = next(entry["result"] for entry in result["history"] if entry.get("tool") == "start_checkout")
        self.assertEqual(result["message"], "Payment is being confirmed. I will not start another payment.")
        self.assertEqual(create_order.call_count, 1)
        self.assertEqual(tools_for("customer_pitch").get_payment_status(checkout["intent_id"])["status"], "PENDING")

        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_pitch", "order_id": checkout["order_id"]}}}}, separators=(",", ":")).encode()
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(ingest_webhook(body, signature, "event_pitch")["accepted"])

        tools = tools_for("customer_pitch")
        self.assertEqual(tools.get_payment_status(checkout["intent_id"])["status"], "CAPTURED")
        timeline = tools.get_audit_timeline(checkout["intent_id"])
        self.assertEqual([event["sequence"] for event in timeline], list(range(1, len(timeline) + 1)))
        self.assertEqual([event["type"] for event in timeline][-2:], ["WEBHOOK_RECEIVED", "ATTEMPT_RESOLVED"])
