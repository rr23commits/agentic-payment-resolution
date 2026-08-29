import os
import unittest
from datetime import datetime, timezone

from backend.catalogue import get_mandate, search_catalogue
from backend.db import migrate
from backend.seed import main as seed


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class DemoSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.update(MANDATE_SIGNING_SECRET="test-secret", MERCHANT_ID="merchant_demo")
        migrate()

    def test_seeded_customer_gets_valid_mandate(self) -> None:
        seed()
        seed()
        mandate = get_mandate("customer_demo")
        self.assertEqual(mandate["id"], "mandate_demo_valid")
        self.assertEqual(mandate["customer_id"], "customer_demo")
        self.assertEqual(mandate["merchant_id"], "merchant_demo")
        self.assertIn("books", mandate["allowed_categories_json"])
        self.assertGreaterEqual(mandate["max_amount_paise"], 40000)
        self.assertGreater(mandate["expires_at"], datetime.now(timezone.utc))
        self.assertEqual([product["id"] for product in search_catalogue("book")], ["product_demo_book"])
