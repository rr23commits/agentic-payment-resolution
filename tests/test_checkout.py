import os
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from psycopg.types.json import Jsonb

from backend.catalogue import create_cart, mandate_token, validate_purchase
from backend.browser_checkout import WAITING_MESSAGE, record_client_payment_reference
from backend.checkout import start_checkout
from backend.db import connect, migrate


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class CheckoutTests(unittest.TestCase):
    customer_id = "customer_checkout"
    mandate_id = "mandate_checkout"

    @classmethod
    def setUpClass(cls) -> None:
        migrate()
        os.environ["MANDATE_SIGNING_SECRET"] = "test-secret"
        os.environ["MERCHANT_ID"] = "merchant_demo"

    def setUp(self) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id=self.customer_id,
            merchant_id="merchant_demo",
            agent_id="agent_1",
            max_amount_paise=50000,
            allowed_categories=["books"],
            expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE audit_events, payment_attempts, checkout_intents, carts, mandates, "
                "products CASCADE"
            )
            cursor.execute(
                "INSERT INTO products VALUES "
                "('product_checkout', 'Book', 'A useful book', 'books', 40000, 3, FALSE)"
            )
            cursor.execute(
                "INSERT INTO mandates VALUES (%s, %s, 'merchant_demo', 'agent_1', "
                "50000, %s, %s, %s)",
                (self.mandate_id, self.customer_id, Jsonb(["books"]), expires_at, token),
            )
        self.cart = create_cart(
            [{"product_id": "product_checkout", "quantity": 1}],
            customer_id=self.customer_id,
        )

    @patch("backend.checkout.create_order", return_value="order_test_1")
    def test_same_client_request_returns_one_intent_and_order(self, create_order) -> None:
        first = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_1", customer_id=self.customer_id
        )
        second = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_1", customer_id=self.customer_id
        )

        self.assertTrue(first["allowed"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(create_order.call_count, 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM checkout_intents")
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute(
                "SELECT sequence FROM audit_events WHERE intent_id = %s ORDER BY sequence",
                (first["intent_id"],),
            )
            self.assertEqual([row[0] for row in cursor], [1, 2])

    @patch("backend.checkout.create_order", return_value="order_concurrent")
    def test_concurrent_requests_for_same_cart_create_one_order(self, create_order) -> None:
        def checkout(number: int) -> dict:
            return start_checkout(
                self.cart["cart_id"], self.mandate_id, f"request_concurrent_{number}",
                customer_id=self.customer_id,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(checkout, (1, 2)))
        self.assertEqual(create_order.call_count, 1)
        self.assertEqual(sum(result["allowed"] for result in results), 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM payment_attempts WHERE status = 'PENDING'")
            self.assertEqual(cursor.fetchone()[0], 1)

    @patch("backend.checkout.create_order", return_value="order_same_key")
    def test_concurrent_replays_of_same_key_create_one_order(self, create_order) -> None:
        def checkout() -> dict:
            return start_checkout(
                self.cart["cart_id"], self.mandate_id, "request_same_key",
                customer_id=self.customer_id,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _unused: checkout(), (1, 2)))
        self.assertEqual(create_order.call_count, 1)
        self.assertEqual(len({result["intent_id"] for result in results}), 1)

    @patch("backend.checkout.create_order", side_effect=RuntimeError("provider unavailable"))
    def test_provider_failure_is_persisted_and_replay_does_not_retry(self, create_order) -> None:
        first = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_provider_failure", customer_id=self.customer_id
        )
        second = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_provider_failure", customer_id=self.customer_id
        )
        self.assertEqual(first["status"], "AMBIGUOUS")
        self.assertFalse(second["allowed"])
        self.assertEqual(second["status"], "AMBIGUOUS")
        self.assertEqual(create_order.call_count, 1)

    @patch("backend.checkout.create_order", return_value="order_test_append_only")
    def test_audit_events_are_contiguous_and_append_only(self, _create_order) -> None:
        checkout = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_append_only", customer_id=self.customer_id
        )
        with self.assertRaises(Exception):
            with connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_events SET type = 'MUTATED' WHERE intent_id = %s",
                    (checkout["intent_id"],),
                )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT sequence FROM audit_events WHERE intent_id = %s ORDER BY sequence",
                (checkout["intent_id"],),
            )
            self.assertEqual([row[0] for row in cursor], [1, 2])

    @patch("backend.checkout.create_order")
    def test_checkout_revalidates_and_creates_no_order_after_cap_changes(self, create_order) -> None:
        self.assertTrue(validate_purchase(self.cart["cart_id"], self.mandate_id)["allowed"])
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(
            customer_id=self.customer_id,
            merchant_id="merchant_demo",
            agent_id="agent_1",
            max_amount_paise=1,
            allowed_categories=["books"],
            expires_at=expires_at,
        )
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE mandates SET max_amount_paise = 1, expires_at = %s, token = %s "
                "WHERE id = %s",
                (expires_at, token, self.mandate_id),
            )

        result = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_2", customer_id=self.customer_id
        )
        self.assertFalse(result["allowed"])
        create_order.assert_not_called()
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM checkout_intents")
            self.assertEqual(cursor.fetchone()[0], 0)

    @patch("backend.checkout.create_order")
    def test_invalid_mandates_cannot_bypass_checkout(self, create_order) -> None:
        cases = [
            {"max_amount_paise": 1, "allowed_categories": ["books"], "expired": False},
            {"max_amount_paise": 50000, "allowed_categories": ["games"], "expired": False},
            {"max_amount_paise": 50000, "allowed_categories": ["books"], "expired": True},
        ]
        for number, case in enumerate(cases):
            expires_at = datetime.now(timezone.utc) + timedelta(
                days=-1 if case["expired"] else 1
            )
            token = mandate_token(
                customer_id=self.customer_id,
                merchant_id="merchant_demo",
                agent_id="agent_1",
                max_amount_paise=case["max_amount_paise"],
                allowed_categories=case["allowed_categories"],
                expires_at=expires_at,
            )
            with connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE mandates SET max_amount_paise = %s, allowed_categories_json = %s, "
                    "expires_at = %s, token = %s WHERE id = %s",
                    (
                        case["max_amount_paise"],
                        Jsonb(case["allowed_categories"]),
                        expires_at,
                        token,
                        self.mandate_id,
                    ),
                )
            result = start_checkout(
                self.cart["cart_id"],
                self.mandate_id,
                f"invalid_request_{number}",
                customer_id=self.customer_id,
            )
            self.assertFalse(result["allowed"])

        create_order.assert_not_called()
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM checkout_intents")
            self.assertEqual(cursor.fetchone()[0], 0)

    @patch("backend.checkout.create_order", return_value="order_test_2")
    def test_non_failed_attempts_block_new_client_requests(self, create_order) -> None:
        start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_3", customer_id=self.customer_id
        )
        pending = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_4", customer_id=self.customer_id
        )
        self.assertFalse(pending["allowed"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE payment_attempts SET status = 'AMBIGUOUS'")
        ambiguous = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_5", customer_id=self.customer_id
        )
        self.assertFalse(ambiguous["allowed"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE payment_attempts SET status = 'CAPTURED'")
        captured = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_6", customer_id=self.customer_id
        )
        self.assertFalse(captured["allowed"])
        self.assertEqual(create_order.call_count, 1)

    @patch("backend.checkout.create_order", return_value="order_test_3")
    def test_client_payment_reference_never_captures_an_attempt(self, create_order) -> None:
        checkout = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_7", customer_id=self.customer_id
        )
        result = record_client_payment_reference(
            checkout["intent_id"],
            self.customer_id,
            checkout["order_id"],
            "pay_browser_1",
        )
        repeated = record_client_payment_reference(
            checkout["intent_id"],
            self.customer_id,
            checkout["order_id"],
            "pay_browser_1",
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["message"], WAITING_MESSAGE)
        self.assertTrue(repeated["idempotent"])
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, razorpay_payment_id FROM payment_attempts WHERE intent_id = %s",
                (checkout["intent_id"],),
            )
            self.assertEqual(cursor.fetchone(), ("PENDING", "pay_browser_1"))
            cursor.execute(
                "SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence",
                (checkout["intent_id"],),
            )
            self.assertEqual([row[0] for row in cursor][-1], "CLIENT_REPORTED")

    @patch("backend.checkout.create_order", return_value="order_test_4")
    def test_wrong_browser_order_reference_changes_nothing(self, create_order) -> None:
        checkout = start_checkout(
            self.cart["cart_id"], self.mandate_id, "request_8", customer_id=self.customer_id
        )
        result = record_client_payment_reference(
            checkout["intent_id"], self.customer_id, "order_wrong", "pay_browser_2"
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["message"], WAITING_MESSAGE)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, razorpay_payment_id FROM payment_attempts WHERE intent_id = %s",
                (checkout["intent_id"],),
            )
            self.assertEqual(cursor.fetchone(), ("PENDING", None))
            cursor.execute(
                "SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence DESC LIMIT 1",
                (checkout["intent_id"],),
            )
            self.assertEqual(cursor.fetchone()[0], "CLIENT_REPORT_REJECTED")
