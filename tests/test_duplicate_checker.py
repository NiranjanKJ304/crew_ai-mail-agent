import sqlite3
import unittest

from fakes import TempDatabaseMixin
from src.services import duplicate_checker as db


class DuplicateCheckerTests(TempDatabaseMixin, unittest.TestCase):
    def test_enqueue_rejects_duplicates(self):
        self.assertTrue(db.enqueue_message("m1"))
        self.assertFalse(db.enqueue_message("m1"))
        self.assertEqual(db.get_pending_message_ids(), ["m1"])

    def test_pending_is_not_processed(self):
        db.enqueue_message("m1")

        self.assertFalse(db.is_processed("m1"))
        self.assertEqual(db.get_status("m1"), db.STATUS_PENDING)

    def test_final_statuses_are_processed(self):
        db.mark_processed("m1")
        db.mark_processed("m2", db.STATUS_SKIPPED, detail="auto_submitted")

        self.assertTrue(db.is_processed("m1"))
        self.assertTrue(db.is_processed("m2"))
        self.assertFalse(db.is_processed("unknown"))
        self.assertEqual(db.get_pending_message_ids(), [])

    def test_enqueue_does_not_reset_processed_message(self):
        db.mark_processed("m1", draft_id="draft-1")

        self.assertFalse(db.enqueue_message("m1"))
        self.assertEqual(db.get_status("m1"), db.STATUS_DRAFT_CREATED)

    def test_failures_retry_then_become_permanent(self):
        db.enqueue_message("m1")

        for _ in range(db.MAX_ATTEMPTS - 1):
            self.assertEqual(db.mark_failed("m1", "ValueError"), db.STATUS_FAILED)
            self.assertIn("m1", db.get_pending_message_ids())

        self.assertEqual(
            db.mark_failed("m1", "ValueError"),
            db.STATUS_FAILED_PERMANENT,
        )
        self.assertTrue(db.is_processed("m1"))
        self.assertEqual(db.get_pending_message_ids(), [])

    def test_pending_ids_are_in_detection_order(self):
        for message_id in ("c", "a", "b"):
            db.enqueue_message(message_id)

        self.assertEqual(db.get_pending_message_ids(), ["c", "a", "b"])

    def test_state_round_trip(self):
        self.assertIsNone(db.get_state("history_id"))
        self.assertEqual(db.get_state("history_id", "x"), "x")

        db.set_state("history_id", 123)
        db.set_state("history_id", 456)

        self.assertEqual(db.get_state("history_id"), "456")

    def test_migrates_original_schema(self):
        self.db_path.unlink()

        with sqlite3.connect(self.db_path) as connection:
            connection.execute("""
                CREATE TABLE processed_emails (
                    message_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO processed_emails VALUES ('old', 'draft_created')"
            )

        db.initialize_database()
        db.initialize_database()  # Safe to run twice.

        with sqlite3.connect(self.db_path) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(processed_emails)"
                )
            }

        self.assertTrue(
            {"attempts", "draft_id", "detail", "updated_at"} <= columns
        )
        self.assertTrue(db.is_processed("old"))


if __name__ == "__main__":
    unittest.main()
