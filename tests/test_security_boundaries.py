import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

from agent.loop import _model_history
from backend.db import connect, migrate
from backend.retention import cleanup_webhook_payloads
from backend.razorpay import fetch_order_payments
from psycopg.types.json import Jsonb


class SecurityBoundaryTests(unittest.TestCase):
    def test_model_history_strips_payment_provider_audit_and_secret_data(self) -> None:
        history = _model_history([
            {
                "tool": "search_catalogue",
                "result": [{"id": "book_1", "name": "Book", "price_paise": 40000}],
            },
            {
                "tool": "create_cart",
                "result": {"cart_id": "cart_1", "customer_id": "customer_1", "total_paise": 40000},
            },
            {
                "tool": "get_mandate",
                "result": {"id": "mandate_1", "max_amount_paise": 50000, "allowed_categories_json": ["books"], "token": "mandate-secret"},
            },
            {
                "tool": "get_payment_status",
                "result": {"found": True, "intent_id": "intent_1", "payment_id": "pay_secret", "status": "PENDING"},
            },
            {
                "tool": "get_audit_timeline",
                "result": [{"type": "WEBHOOK_RECEIVED", "payload": {"razorpay_signature": "webhook-secret"}}],
            },
        ])
        serialized = json.dumps(history)
        self.assertIn("book_1", serialized)
        self.assertIn("cart_1", serialized)
        self.assertIn("mandate_1", serialized)
        for secret in ("pay_secret", "webhook-secret", "mandate-secret", "customer_1"):
            self.assertNotIn(secret, serialized)

    def test_frontend_dynamic_values_use_text_nodes_not_html(self) -> None:
        source = (Path(__file__).parents[1] / "frontend" / "app.js").read_text()
        self.assertNotIn("innerHTML", source)
        self.assertIn("textContent", source)
        self.assertIn("createElement", source)

    @patch("backend.razorpay.urlopen")
    def test_razorpay_payment_path_quotes_order_id(self, urlopen) -> None:
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda *args: None})()
        response.read = lambda: b'{"items": []}'
        urlopen.return_value = response
        with patch.dict(os.environ, {"RAZORPAY_KEY_ID": "rzp_test_key", "RAZORPAY_KEY_SECRET": "secret"}):
            fetch_order_payments("order/a?b")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://api.razorpay.com/v1/orders/order%2Fa%3Fb/payments")

    def test_postgres_compose_defaults_are_loopback_and_password_is_required(self) -> None:
        source = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
        self.assertIn("127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432", source)
        self.assertIn("POSTGRES_PASSWORD:?", source)
        self.assertNotIn('"5432:5432"', source)


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()

    def setUp(self) -> None:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events CASCADE")
            cursor.execute(
                "INSERT INTO webhook_events (provider_event_id, payload_json, signature_valid, processed_at) VALUES "
                "('old_event', %s, TRUE, %s), ('pending_event', %s, TRUE, NULL)",
                (Jsonb({"secret": "provider"}), datetime.now(timezone.utc) - timedelta(days=31), Jsonb({"secret": "pending"})),
            )

    def test_cleanup_redacts_old_processed_payloads_but_keeps_pending(self) -> None:
        self.assertEqual(cleanup_webhook_payloads(30), 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT provider_event_id, payload_json FROM webhook_events ORDER BY provider_event_id")
            rows = cursor.fetchall()
        self.assertEqual(rows[0][1], {"retained": False, "provider_event_id": "old_event"})
        self.assertEqual(rows[1][1], {"secret": "pending"})
