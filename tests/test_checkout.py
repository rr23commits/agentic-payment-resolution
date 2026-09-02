import os
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from psycopg.types.json import Jsonb

from backend.catalogue import create_cart, mandate_token, validate_purchase
from backend.browser_checkout import WAITING_MESSAGE, record_client_cancellation, record_client_payment_reference, record_client_timeout
from backend.resolver import _client_evidence, _provider_evidence, resolve_attempt
from backend.checkout import start_checkout
from backend.db import connect, migrate
from backend.razorpay import RazorpayError


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

    @patch("backend.checkout.create_order", return_value="order_customer_wide")
    def test_unresolved_attempt_blocks_different_cart_for_same_customer(self, create_order) -> None:
        second_cart = create_cart([{"product_id": "product_checkout", "quantity": 1}], customer_id=self.customer_id)
        first = start_checkout(self.cart["cart_id"], self.mandate_id, "request_cart_a", customer_id=self.customer_id)
        second = start_checkout(second_cart["cart_id"], self.mandate_id, "request_cart_b", customer_id=self.customer_id)
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(create_order.call_count, 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM payment_attempts")
            self.assertEqual(cursor.fetchone()[0], 1)

    @patch("backend.checkout.create_order", return_value="order_lock_order")
    def test_checkout_and_resolver_paths_complete_without_lock_order_deadlock(self, _create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], self.mandate_id, "request_lock_order", customer_id=self.customer_id)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts WHERE intent_id = %s", (checkout["intent_id"],))
            attempt_id = cursor.fetchone()[0]

        def checkout_again() -> dict:
            return start_checkout(self.cart["cart_id"], self.mandate_id, "request_lock_order_again", customer_id=self.customer_id)

        def resolve_timeout() -> dict:
            return resolve_attempt(attempt_id, _client_evidence("timeout"))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(checkout_again), executor.submit(resolve_timeout)]
            results = [future.result(timeout=5) for future in futures]
        self.assertTrue(any(result.get("status") == "AMBIGUOUS" for result in results))

    @patch("backend.checkout.create_order", side_effect=RazorpayError("provider unavailable"))
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

    @patch("backend.checkout.create_order", side_effect=RuntimeError("programming error"))
    def test_unexpected_provider_exception_becomes_ambiguous_without_retry(self, create_order) -> None:
        result = start_checkout(self.cart["cart_id"], self.mandate_id, "request_unexpected_provider_error", customer_id=self.customer_id)
        replay = start_checkout(self.cart["cart_id"], self.mandate_id, "request_unexpected_provider_error", customer_id=self.customer_id)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertFalse(replay["allowed"])
        self.assertEqual(replay["status"], "AMBIGUOUS")
        self.assertEqual(create_order.call_count, 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status, razorpay_order_id, stock_reserved FROM payment_attempts")
            self.assertEqual(cursor.fetchone(), ("AMBIGUOUS", None, True))
            cursor.execute("SELECT type, payload_json FROM audit_events WHERE type = 'CHECKOUT_PROVIDER_FAILURE'")
            event = cursor.fetchone()
            self.assertEqual(event[0], "CHECKOUT_PROVIDER_FAILURE")
            self.assertEqual(event[1]["exception_type"], "RuntimeError")

    @patch("backend.checkout.create_order", return_value="order_stock_a")
    def test_only_one_customer_can_checkout_the_last_item(self, create_order) -> None:
        second_customer = "customer_stock_other"
        second_mandate = "mandate_stock_other"
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        token = mandate_token(customer_id=second_customer, merchant_id="merchant_demo", agent_id="agent_1", max_amount_paise=50000, allowed_categories=["books"], expires_at=expires_at)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE products SET stock = 1 WHERE id = 'product_checkout'")
            cursor.execute("INSERT INTO mandates VALUES (%s, %s, 'merchant_demo', 'agent_1', 50000, %s, %s, %s)", (second_mandate, second_customer, Jsonb(["books"]), expires_at, token))
        second_cart = create_cart([{"product_id": "product_checkout", "quantity": 1}], customer_id=second_customer)
        carts = [(self.cart["cart_id"], self.mandate_id, self.customer_id), (second_cart["cart_id"], second_mandate, second_customer)]
        def checkout(args: tuple[str, str, str]) -> dict:
            cart_id, mandate_id, customer_id = args
            return start_checkout(cart_id, mandate_id, f"stock_{customer_id}", customer_id=customer_id)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(checkout, carts))
        self.assertEqual(sum(result["allowed"] for result in results), 1)
        self.assertEqual(create_order.call_count, 1)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT stock FROM products WHERE id = 'product_checkout'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_cart_rejects_excessive_quantities_and_totals(self) -> None:
        with self.assertRaisesRegex(ValueError, "Quantity"):
            create_cart([{"product_id": "product_checkout", "quantity": 1001}], customer_id=self.customer_id)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE products SET price_paise = 9000000000000000000, stock = 2 WHERE id = 'product_checkout'")
        with self.assertRaisesRegex(ValueError, "total"):
            create_cart([{"product_id": "product_checkout", "quantity": 2}], customer_id=self.customer_id)

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
            self.assertEqual(cursor.fetchone(), ("PENDING", None))
            cursor.execute(
                "SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence",
                (checkout["intent_id"],),
            )
            self.assertEqual([row[0] for row in cursor][-1], "CLIENT_REPORTED")
            cursor.execute(
                "SELECT payload_json->>'client_payment_id' FROM audit_events "
                "WHERE intent_id = %s AND type = 'CLIENT_REPORTED'",
                (checkout["intent_id"],),
            )
            self.assertEqual(cursor.fetchone()[0], "pay_browser_1")

    @patch("backend.checkout.create_order", side_effect=["order_dismissed", "order_after_dismissal"])
    def test_modal_cancellation_before_payment_abandons_and_allows_new_checkout(self, create_order) -> None:
        first = start_checkout(self.cart["cart_id"], self.mandate_id, "request_abandoned", customer_id=self.customer_id)
        cancelled = record_client_cancellation(first["intent_id"], self.customer_id)
        self.assertTrue(cancelled["accepted"])
        self.assertEqual(cancelled["status"], "ABANDONED")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status, stock_reserved FROM payment_attempts WHERE intent_id = %s", (first["intent_id"],))
            self.assertEqual(cursor.fetchone(), ("ABANDONED", False))
            cursor.execute("SELECT stock FROM products WHERE id = 'product_checkout'")
            self.assertEqual(cursor.fetchone()[0], 3)

        retry = start_checkout(self.cart["cart_id"], self.mandate_id, "request_retry", customer_id=self.customer_id)
        self.assertTrue(retry["allowed"])
        self.assertEqual(create_order.call_count, 2)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT type FROM audit_events WHERE intent_id = %s ORDER BY sequence DESC LIMIT 1", (first["intent_id"],))
            self.assertEqual(cursor.fetchone()[0], "ATTEMPT_RESOLVED")

    @patch("backend.checkout.create_order", return_value="order_cancel_blocked")
    def test_cancellation_does_not_close_submitted_or_ambiguous_attempts(self, create_order) -> None:
        submitted = start_checkout(self.cart["cart_id"], self.mandate_id, "request_submitted", customer_id=self.customer_id)
        record_client_payment_reference(submitted["intent_id"], self.customer_id, submitted["order_id"], "pay_submitted")
        self.assertFalse(record_client_cancellation(submitted["intent_id"], self.customer_id)["accepted"])
        blocked = start_checkout(self.cart["cart_id"], self.mandate_id, "request_submitted_retry", customer_id=self.customer_id)
        self.assertFalse(blocked["allowed"])
        self.assertEqual(create_order.call_count, 1)

        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE payment_attempts SET status = 'AMBIGUOUS' WHERE intent_id = %s", (submitted["intent_id"],))
            cursor.execute("UPDATE checkout_intents SET status = 'AMBIGUOUS' WHERE id = %s", (submitted["intent_id"],))
        self.assertFalse(record_client_cancellation(submitted["intent_id"], self.customer_id)["accepted"])

    @patch("backend.checkout.create_order", return_value="order_late_capture")
    def test_authorized_evidence_stays_pending_then_late_capture_resolves_same_attempt(self, create_order) -> None:
        checkout = start_checkout(self.cart["cart_id"], self.mandate_id, "request_late_capture", customer_id=self.customer_id)
        attempt_id = checkout["intent_id"]
        record_client_payment_reference(attempt_id, self.customer_id, checkout["order_id"], "pay_authorized")
        record_client_cancellation(attempt_id, self.customer_id)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts WHERE intent_id = %s", (attempt_id,))
            payment_attempt_id = cursor.fetchone()[0]
            cursor.execute("SELECT stock FROM products WHERE id = 'product_checkout'")
            self.assertEqual(cursor.fetchone()[0], 2)
        authorized = resolve_attempt(
            payment_attempt_id,
            _provider_evidence("RAZORPAY_WEBHOOK", order_id=checkout["order_id"], payment_id="pay_authorized", status="authorized"),
        )
        self.assertEqual(authorized["status"], "PENDING")
        self.assertTrue(authorized["reconciliation_required"])
        retry = start_checkout(self.cart["cart_id"], self.mandate_id, "request_authorized_retry", customer_id=self.customer_id)
        self.assertFalse(retry["allowed"])
        self.assertEqual(create_order.call_count, 1)
        resolved = resolve_attempt(
            payment_attempt_id,
            _provider_evidence("RAZORPAY_WEBHOOK", order_id=checkout["order_id"], payment_id="pay_captured", status="captured"),
        )
        self.assertEqual(resolved["status"], "CAPTURED")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status, stock_reserved, razorpay_payment_id FROM payment_attempts WHERE id = %s", (payment_attempt_id,))
            self.assertEqual(cursor.fetchone(), ("CAPTURED", False, "pay_captured"))

    @patch("backend.checkout.create_order", side_effect=["order_failed", "order_retry"])
    def test_authoritative_failure_releases_stock_and_allows_new_checkout(self, create_order) -> None:
        first = start_checkout(self.cart["cart_id"], self.mandate_id, "request_failure", customer_id=self.customer_id)
        record_client_payment_reference(first["intent_id"], self.customer_id, first["order_id"], "pay_failed")
        record_client_cancellation(first["intent_id"], self.customer_id)
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM payment_attempts WHERE intent_id = %s", (first["intent_id"],))
            attempt_id = cursor.fetchone()[0]
        resolved = resolve_attempt(
            attempt_id,
            _provider_evidence("RAZORPAY_WEBHOOK", order_id=first["order_id"], payment_id="pay_failed", status="failed"),
        )
        self.assertEqual(resolved["status"], "FAILED")
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT stock FROM products WHERE id = 'product_checkout'")
            self.assertEqual(cursor.fetchone()[0], 3)

        retry = start_checkout(self.cart["cart_id"], self.mandate_id, "request_after_failure", customer_id=self.customer_id)
        self.assertTrue(retry["allowed"])
        self.assertEqual(create_order.call_count, 2)

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
