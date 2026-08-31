import hashlib
import hmac
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from psycopg.types.json import Jsonb

from backend.catalogue import create_cart, mandate_token
from backend.browser_checkout import record_client_payment_reference, record_client_timeout
from backend.checkout import start_checkout
from backend.db import connect, migrate
from backend.resolver import _client_evidence, reconcile_status, resolve_attempt
from backend.webhooks import ingest_webhook


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class WebhookTests(unittest.TestCase):
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
            customer_id="customer_webhook", merchant_id="merchant_demo", agent_id="agent_1",
            max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
            cursor.execute("INSERT INTO products VALUES ('product_webhook', 'Book', 'Book', 'books', 40000, 1, FALSE)")
            cursor.execute("INSERT INTO mandates VALUES ('mandate_webhook', 'customer_webhook', 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (Jsonb(["books"]), expires_at, token))
        self.cart = create_cart([{"product_id": "product_webhook", "quantity": 1}], customer_id="customer_webhook")

    def _event(self, order_id: str, event: str = "payment.captured") -> tuple[bytes, str]:
        body = json.dumps({"event": event, "payload": {"payment": {"entity": {"id": "pay_webhook", "order_id": order_id}}}}, separators=(",", ":")).encode()
        return body, hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    @patch("backend.checkout.create_order", return_value="order_webhook")
    def test_verified_webhook_is_audited_once(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_webhook", customer_id="customer_webhook")
        body, signature = self._event(checkout["order_id"])
        self.assertTrue(ingest_webhook(body, signature, "event_1")["accepted"])
        self.assertTrue(ingest_webhook(body, signature, "event_1")["idempotent"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts")
            self.assertEqual(cursor.fetchone()[0], "CAPTURED")
            cursor.execute("SELECT COUNT(*) FROM webhook_events")
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute("SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence", (checkout["intent_id"],))
            self.assertEqual([row[0] for row in cursor][-2:], ["WEBHOOK_RECEIVED", "ATTEMPT_RESOLVED"])

    @patch("backend.checkout.create_order")
    def test_webhook_arriving_during_order_creation_is_processed_once(self, create_order) -> None:
        body = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"id": "pay_early", "order_id": "order_early"}}},
        }, separators=(",", ":")).encode()
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

        def create_and_deliver(_amount: int, _receipt: str) -> str:
            result = ingest_webhook(body, signature, "event_early")
            self.assertTrue(result["accepted"])
            self.assertFalse(result["matched"])
            return "order_early"

        create_order.side_effect = create_and_deliver
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_early", customer_id="customer_webhook")
        self.assertEqual(checkout["status"], "CAPTURED")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], "CAPTURED")
            cursor.execute("SELECT processed_at FROM webhook_events WHERE provider_event_id = 'event_early'")
            self.assertIsNotNone(cursor.fetchone()[0])

    @patch("backend.checkout.create_order", return_value="order_authorized")
    def test_authorized_webhook_resolves_to_captured(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_authorized", customer_id="customer_webhook")
        body, signature = self._event(checkout["order_id"], "payment.authorized")
        self.assertTrue(ingest_webhook(body, signature, "event_authorized")["accepted"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], "CAPTURED")

    @patch("backend.checkout.create_order", return_value="order_client_then_provider")
    def test_provider_payment_replaces_arbitrary_client_reference(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_client_then_provider", customer_id="customer_webhook")
        self.assertTrue(record_client_payment_reference(checkout["intent_id"], "customer_webhook", checkout["order_id"], "pay_attacker")["accepted"])
        body, signature = self._event(checkout["order_id"])
        ingest_webhook(body, signature, "event_client_then_provider")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status, razorpay_payment_id FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone(), ("CAPTURED", "pay_webhook"))

    @patch("backend.resolver.fetch_order_payments")
    @patch("backend.checkout.create_order", return_value="order_reconcile_race")
    def test_webhook_wins_reconciliation_race(self, _create_order, fetch_payments) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_reconcile_race", customer_id="customer_webhook")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            attempt_id = cursor.fetchone()[0]
        fetched = threading.Event()
        release = threading.Event()

        def fetch_then_pause(order_id: str) -> list[dict]:
            fetched.set()
            self.assertTrue(release.wait(5))
            return [{"id": "pay_reconcile", "order_id": order_id, "status": "failed"}]

        fetch_payments.side_effect = fetch_then_pause
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(reconcile_status, attempt_id)
            self.assertTrue(fetched.wait(5))
            body, signature = self._event(checkout["order_id"])
            self.assertEqual(ingest_webhook(body, signature, "event_reconcile_race")["accepted"], True)
            release.set()
            self.assertEqual(future.result(timeout=5)["status"], "CAPTURED")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts WHERE id = %s", (attempt_id,))
            self.assertEqual(cursor.fetchone()[0], "CAPTURED")
            cursor.execute("SELECT COUNT(*) FROM audit_events WHERE intent_id = %s AND type = 'RECONCILIATION_CHECKED'", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], 0)

    @patch("backend.checkout.create_order", side_effect=["order_mismatch_a", "order_mismatch_b"])
    def test_webhook_order_and_payment_from_different_attempts_is_not_correlated(self, _create_order) -> None:
        second_customer = "customer_webhook_other"
        second_mandate = "mandate_webhook_other"
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(customer_id=second_customer, merchant_id="merchant_demo", agent_id="agent_1", max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE products SET stock = 2 WHERE id = 'product_webhook'")
            cursor.execute("INSERT INTO mandates VALUES (%s, %s, 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (second_mandate, second_customer, Jsonb(["books"]), expires_at, token))
        second_cart = create_cart([{"product_id": "product_webhook", "quantity": 1}], customer_id=second_customer)
        first = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_mismatch_a", customer_id="customer_webhook")
        second = start_checkout(second_cart["cart_id"], second_mandate, "request_mismatch_b", customer_id=second_customer)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE payment_attempts SET razorpay_payment_id = 'pay_mismatch_b' WHERE intent_id = %s", (second["intent_id"],))
        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_mismatch_b", "order_id": first["order_id"]}}}}, separators=(",", ":")).encode()
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        result = ingest_webhook(body, signature, "event_mismatch")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["matched"])
        replay = ingest_webhook(body, signature, "event_mismatch")
        self.assertTrue(replay["idempotent"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts ORDER BY id")
            self.assertEqual({row[0] for row in cursor}, {"PENDING"})
            cursor.execute("SELECT type FROM audit_events WHERE type = 'WEBHOOK_CONTRADICTION'")
            self.assertEqual(cursor.fetchone()[0], "WEBHOOK_CONTRADICTION")

    @patch("backend.checkout.create_order", return_value="order_conflict")
    def test_conflicting_provider_references_are_audited_without_transition(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_conflict", customer_id="customer_webhook")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payment_attempts SET razorpay_payment_id = 'pay_known' WHERE intent_id = %s",
                (checkout["intent_id"],),
            )
        body, _ = self._event(checkout["order_id"])
        body = body.replace(b"pay_webhook", b"pay_other")
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
        result = ingest_webhook(body, signature, "event_conflict")
        self.assertTrue(result["accepted"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], "PENDING")
            cursor.execute("SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchall()[-1][0], "RESOLUTION_EXCEPTION")

    def test_invalid_signature_changes_nothing(self) -> None:
        result = ingest_webhook(b'{"event":"payment.captured"}', "bad", "event_invalid")
        self.assertFalse(result["accepted"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM webhook_events")
            self.assertEqual(cursor.fetchone()[0], 0)

    @patch("backend.checkout.create_order", return_value="order_webhook")
    def test_resolver_preserves_invalid_state_and_allows_reversal(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_resolver", customer_id="customer_webhook")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts")
            attempt_id = cursor.fetchone()[0]

        rejected = resolve_attempt(attempt_id, {"source": "CLIENT_REPORTED", "event": "captured"})
        self.assertEqual(rejected["status"], "PENDING")
        fabricated = resolve_attempt(attempt_id, {"source": "RAZORPAY_WEBHOOK", "verified": True, "event": "payment.captured", "order_id": checkout["order_id"], "payment_id": "pay_fabricated"})
        self.assertEqual(fabricated["status"], "PENDING")
        ambiguous = resolve_attempt(attempt_id, _client_evidence("timeout"))
        self.assertEqual(ambiguous["status"], "AMBIGUOUS")
        body, signature = self._event(checkout["order_id"])
        ingest_webhook(body, signature, "event_resolver_capture")
        body, signature = self._event(checkout["order_id"], "payment.reversed")
        ingest_webhook(body, signature, "event_resolver_reverse")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts WHERE id = %s", (attempt_id,))
            self.assertEqual(cursor.fetchone()[0], "REVERSED")
            cursor.execute("SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence", (checkout["intent_id"],))
            self.assertIn("RESOLUTION_EXCEPTION", [row[0] for row in cursor])

    @patch("backend.resolver.fetch_order_payments")
    @patch("backend.checkout.create_order", return_value="order_webhook")
    def test_reconciliation_resolves_the_existing_attempt(self, _create_order, fetch_payments) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_reconcile", customer_id="customer_webhook")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts")
            attempt_id = cursor.fetchone()[0]
        fetch_payments.return_value = [{"id": "pay_reconciled", "order_id": checkout["order_id"], "status": "failed"}]

        result = reconcile_status(attempt_id)
        self.assertEqual(result["status"], "FAILED")
        replay = reconcile_status(attempt_id)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(fetch_payments.call_count, 2)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM audit_events WHERE intent_id = %s AND type = 'ATTEMPT_RESOLVED'", (checkout["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], 1)

    @patch("backend.checkout.create_order", return_value="order_timeout")
    def test_demo_timeout_uses_resolver_and_marks_pending_ambiguous(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_timeout", customer_id="customer_webhook")
        result = record_client_timeout(checkout["intent_id"], "customer_webhook")
        self.assertEqual(result["status"], "AMBIGUOUS")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM payment_attempts")
            self.assertEqual(cursor.fetchone()[0], "AMBIGUOUS")

    @patch("backend.resolver.fetch_order_payments", return_value=[])
    @patch("backend.checkout.create_order", return_value="order_unobserved")
    def test_reconciliation_without_payment_is_audited(self, _create_order, _fetch_payments) -> None:
        checkout = start_checkout(self.cart["cart_id"], "mandate_webhook", "request_unobserved", customer_id="customer_webhook")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts")
            attempt_id = cursor.fetchone()[0]
        result = reconcile_status(attempt_id)
        self.assertFalse(result["observed"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence",
                (checkout["intent_id"],),
            )
            self.assertEqual(cursor.fetchall()[-1][0], "RECONCILIATION_CHECKED")
