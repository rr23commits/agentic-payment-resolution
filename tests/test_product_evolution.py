import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.catalogue import create_cart, increase_mandate, merchant_catalogue, search_catalogue, update_mandate, validate_purchase
from backend.checkout import start_checkout
from backend.db import connect, migrate
from backend.seed import main as seed
from backend.views import merchant_metrics


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class ProductEvolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(MANDATE_SIGNING_SECRET="test-secret", MERCHANT_ID="merchant_demo")
        migrate()

    def setUp(self):
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE webhook_events, audit_events, payment_attempts, checkout_intents, carts, mandates, products CASCADE")
        seed()

    def test_catalogue_includes_savings_and_explicit_recommendation_reason(self):
        book = next(product for product in search_catalogue("book") if product["id"] == "product_demo_book")
        self.assertEqual(book["savings_paise"], 5000)
        self.assertEqual(book["recommendations"][0]["id"], "product_demo_notebook")
        self.assertTrue(book["recommendations"][0]["reason"])

    def test_ai_catalogue_is_safe_and_read_only(self):
        catalogue = merchant_catalogue()
        self.assertEqual(len(catalogue["products"]), 10)
        restricted = next(product for product in catalogue["products"] if product["id"] == "product_demo_restricted")
        self.assertFalse(restricted["eligible"])
        self.assertNotIn("token", catalogue)
        self.assertTrue(catalogue["checkout_requirements"]["single_cart"])

    def test_metrics_has_compact_growth_shape(self):
        metrics = merchant_metrics()
        self.assertEqual(metrics["carts"], 0)
        self.assertIn("duplicate_charges_prevented", metrics)
        self.assertIn("ambiguous_payments_resolved", metrics)

    def test_over_cap_cart_can_be_revalidated_after_mandate_increase(self):
        cart = create_cart([{"product_id": "product_demo_book", "quantity": 1}, {"product_id": "product_demo_notebook", "quantity": 1}], customer_id="customer_demo")
        before = validate_purchase(cart["cart_id"], "mandate_demo_valid", customer_id="customer_demo")
        self.assertFalse(before["allowed"])
        self.assertIn("exceeds mandate cap", " ".join(before["reasons"]))
        increased = increase_mandate("customer_demo", "mandate_demo_valid", cart["cart_id"], request_id="ux-increase")
        self.assertGreaterEqual(increased["max_amount_paise"], cart["total_paise"])
        after = validate_purchase(cart["cart_id"], increased["id"], customer_id="customer_demo")
        self.assertTrue(after["allowed"])

    def test_checkout_stays_blocked_until_mandate_is_sufficient(self):
        cart = create_cart([{"product_id": "product_demo_book", "quantity": 1}, {"product_id": "product_demo_notebook", "quantity": 1}], customer_id="customer_demo")
        blocked = start_checkout(cart["cart_id"], "mandate_demo_valid", "ux-blocked", customer_id="customer_demo")
        self.assertFalse(blocked["allowed"])
        increased = increase_mandate("customer_demo", "mandate_demo_valid", cart["cart_id"], request_id="ux-checkout-increase")
        with patch("backend.checkout.create_order", return_value="order_ux"):
            allowed = start_checkout(cart["cart_id"], increased["id"], "ux-allowed", customer_id="customer_demo")
        self.assertTrue(allowed["allowed"])
