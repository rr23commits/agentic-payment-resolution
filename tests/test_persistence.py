import os
import unittest

from backend.db import connect, migrate


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is not set")
class PersistenceSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()

    def test_initial_tables_exist(self) -> None:
        expected = {
            "products",
            "mandates",
            "carts",
            "checkout_intents",
            "payment_attempts",
            "webhook_events",
            "audit_events",
        }
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            self.assertTrue(expected <= {row[0] for row in cursor})

    def test_safety_constraints_exist(self) -> None:
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
            indexes = {row[0] for row in cursor}
            cursor.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit_events'::regclass"
            )
            triggers = {row[0] for row in cursor}

        self.assertIn("payment_attempts_one_active_per_intent", indexes)
        self.assertIn("audit_events_append_only", triggers)
