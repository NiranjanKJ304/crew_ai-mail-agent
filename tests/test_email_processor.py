import unittest

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from fakes import (
    FakeGmailService,
    TempDatabaseMixin,
    history_page,
    http_error,
    make_message,
)
from src.services import duplicate_checker as db
from src.services import email_processor
from src.services.email_processor import EmailProcessor

ACCOUNT = "agent@college.edu"
START_TIME = 1_767_225_600


class FakeClock:
    def __init__(self, now=START_TIME):
        self.now = now

    def __call__(self):
        return self.now


class FakeReplyGenerator:
    def __init__(self, reply="Thank you. I will check and get back to you."):
        self.reply = reply
        self.inputs = []
        self.errors = []

    def __call__(self, email_content):
        self.inputs.append(email_content)

        if self.errors:
            raise self.errors.pop(0)

        return self.reply


class ProcessorTestCase(TempDatabaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = FakeGmailService(email=ACCOUNT, history_id="100")
        self.generator = FakeReplyGenerator()
        self.clock = FakeClock()
        self.processor = self.new_processor()

    def new_processor(self):
        return EmailProcessor(
            self.service,
            ACCOUNT,
            reply_generator=self.generator,
            clock=self.clock,
        )

    def add_message(self, message_id, **kwargs):
        self.service.messages[message_id] = make_message(message_id, **kwargs)


class CheckpointTests(ProcessorTestCase):
    def test_first_start_uses_current_history_id(self):
        history_id, resumed = self.processor.initialize_checkpoint()

        self.assertEqual(history_id, "100")
        self.assertFalse(resumed)
        self.assertEqual(db.get_state("history_id"), "100")
        self.assertEqual(db.get_state("account_email"), ACCOUNT)

    def test_existing_emails_are_not_processed_on_startup(self):
        # An email already in the mailbox before the app started.
        self.add_message("old")
        self.processor.initialize_checkpoint()

        summary = self.processor.poll_once()

        self.assertEqual(summary["detected"], 0)
        self.assertEqual(self.service.created_drafts, [])
        self.assertIsNone(db.get_status("old"))

    def test_restart_resumes_saved_checkpoint(self):
        self.processor.initialize_checkpoint()
        db.set_state("history_id", "150")
        self.service.profile["historyId"] = "999"

        history_id, resumed = self.new_processor().initialize_checkpoint()

        self.assertEqual(history_id, "150")
        self.assertTrue(resumed)

    def test_different_account_resets_checkpoint(self):
        self.processor.initialize_checkpoint()
        db.set_state("account_email", "someone@else.edu")
        self.service.profile["historyId"] = "300"

        history_id, resumed = self.new_processor().initialize_checkpoint()

        self.assertEqual(history_id, "300")
        self.assertFalse(resumed)


class MonitoringTestCase(ProcessorTestCase):
    """Starts each test after the startup checkpoint has been saved."""

    def setUp(self):
        super().setUp()
        self.processor.initialize_checkpoint()


class PollTests(MonitoringTestCase):
    def test_new_email_creates_draft_and_advances_checkpoint(self):
        self.add_message("m1", thread_id="t1")
        self.service.set_history(None, history_page(["m1"], "110"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["detected"], 1)
        self.assertEqual(summary["draft_created"], 1)
        self.assertEqual(len(self.service.created_drafts), 1)
        self.assertEqual(
            self.service.created_drafts[0]["message"]["threadId"],
            "t1",
        )
        self.assertEqual(db.get_status("m1"), db.STATUS_DRAFT_CREATED)
        self.assertEqual(db.get_state("history_id"), "110")
        self.assertEqual(self.service.send_calls, [])

    def test_reply_generator_receives_email_content(self):
        self.add_message("m1", subject="Exam", body="When is the exam?")
        self.service.set_history(None, history_page(["m1"], "110"))

        self.processor.poll_once()

        content = self.generator.inputs[0]
        self.assertIn("Subject: Exam", content)
        self.assertIn("When is the exam?", content)
        self.assertIn("asha@example.com", content)

    def test_duplicate_notifications_create_one_draft(self):
        self.add_message("m1")
        self.service.set_history(
            None,
            history_page(["m1", "m1"], "110"),
            history_page(["m1"], "120"),
        )

        first = self.processor.poll_once()
        second = self.processor.poll_once()

        self.assertEqual(first["draft_created"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["draft_created"], 0)
        self.assertEqual(len(self.service.created_drafts), 1)
        self.assertEqual(len(self.generator.inputs), 1)

    def test_paginated_history_processes_every_message(self):
        for message_id in ("a", "b", "c"):
            self.add_message(message_id)

        self.service.set_history(None, history_page(["a", "b"], "130", "p2"))
        self.service.set_history("p2", history_page(["c"], "130"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["detected"], 3)
        self.assertEqual(summary["draft_created"], 3)
        self.assertEqual(db.get_state("history_id"), "130")

    def test_restart_does_not_create_duplicate_drafts(self):
        self.add_message("m1")
        self.service.set_history(None, history_page(["m1"], "110"))
        self.processor.poll_once()

        restarted = self.new_processor()
        restarted.initialize_checkpoint()
        self.service.set_history(None, history_page(["m1"], "110"))
        summary = restarted.poll_once()

        self.assertEqual(summary["draft_created"], 0)
        self.assertEqual(len(self.service.created_drafts), 1)

    def test_existing_draft_from_interrupted_run_is_reused(self):
        # Simulates a crash after the draft was created but before the
        # database was updated: the message is still "pending".
        self.add_message("m1", thread_id="t1")
        db.enqueue_message("m1")
        self.service.threads["t1"] = {"messages": [{
            "id": "draft-msg",
            "labelIds": ["DRAFT"],
            "payload": {"headers": [
                {"name": "In-Reply-To", "value": "<m1@mail.example.com>"},
            ]},
        }]}

        summary = self.processor.poll_once()

        self.assertEqual(summary["already_handled"], 1)
        self.assertEqual(self.service.created_drafts, [])
        self.assertEqual(self.generator.inputs, [])
        self.assertTrue(db.is_processed("m1"))


class SkipTests(MonitoringTestCase):
    def assert_skipped(self, reason, **message_kwargs):
        self.add_message("m1", **message_kwargs)
        self.service.set_history(None, history_page(["m1"], "110"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.service.created_drafts, [])
        self.assertEqual(self.generator.inputs, [])
        self.assertTrue(db.is_processed("m1"))

        with db._connect() as connection:
            detail = connection.execute(
                "SELECT detail FROM processed_emails WHERE message_id='m1'"
            ).fetchone()[0]

        self.assertEqual(detail, reason)

    def test_skips_email_from_authenticated_account(self):
        self.assert_skipped(
            "sent_by_authenticated_account",
            sender=f"Me <{ACCOUNT.upper()}>",
        )

    def test_skips_sent_label(self):
        self.assert_skipped(
            "sent_by_authenticated_account",
            labels=("INBOX", "SENT"),
        )

    def test_skips_auto_submitted(self):
        self.assert_skipped(
            "auto_submitted",
            extra_headers={"Auto-Submitted": "auto-replied"},
        )

    def test_skips_bulk_mail(self):
        self.assert_skipped("bulk_mail", extra_headers={"Precedence": "bulk"})

    def test_skips_no_reply_sender(self):
        self.assert_skipped("no_reply_sender", sender="no-reply@example.com")

    def test_skips_missing_sender(self):
        self.assert_skipped("missing_sender", sender="")

    def test_skips_spam(self):
        self.assert_skipped("spam_or_trash", labels=("SPAM",))

    def test_skips_reply_to_self(self):
        self.assert_skipped(
            "reply_address_is_authenticated_account",
            extra_headers={"Reply-To": ACCOUNT},
        )

    def test_skips_deleted_message(self):
        self.service.set_history(None, history_page(["gone"], "110"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(db.get_status("gone"), db.STATUS_SKIPPED)

    def test_auto_submitted_no_is_processed(self):
        self.add_message("m1", extra_headers={"Auto-Submitted": "no"})
        self.service.set_history(None, history_page(["m1"], "110"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["draft_created"], 1)


class ErrorHandlingTests(MonitoringTestCase):
    def test_history_server_error_keeps_checkpoint(self):
        self.add_message("m1")
        self.service.set_history(
            None,
            http_error(500),
            history_page(["m1"], "110"),
        )

        with self.assertRaises(HttpError):
            self.processor.poll_once()

        self.assertEqual(db.get_state("history_id"), "100")

        summary = self.processor.poll_once()

        self.assertEqual(summary["draft_created"], 1)
        self.assertEqual(db.get_state("history_id"), "110")

    def test_expired_history_recovers_recent_messages(self):
        self.add_message("m1")
        self.service.set_history(None, http_error(404))
        self.service.profile["historyId"] = "500"
        self.service.message_list_pages = {None: {"messages": [{"id": "m1"}]}}
        self.clock.now = START_TIME + 3600

        summary = self.processor.poll_once()

        self.assertEqual(summary["draft_created"], 1)
        self.assertEqual(db.get_state("history_id"), "500")

        query = self.service.message_list_calls[0]["q"]
        since = int(query.rsplit("after:", 1)[1])
        # Never looks back before the app first started.
        self.assertGreaterEqual(since, START_TIME)

    def test_expired_history_lookback_is_capped(self):
        self.service.set_history(None, http_error(404))
        self.clock.now = START_TIME + 30 * 24 * 3600
        db.set_state("last_sync_at", START_TIME)

        self.processor.poll_once()

        query = self.service.message_list_calls[0]["q"]
        since = int(query.rsplit("after:", 1)[1])
        self.assertEqual(
            since,
            self.clock.now - email_processor.MAX_RECOVERY_SECONDS,
        )

    def test_transient_draft_error_retries_without_counting_attempt(self):
        self.add_message("m1")
        self.service.set_history(None, history_page(["m1"], "110"))
        self.service.draft_errors.push(http_error(503))

        first = self.processor.poll_once()

        self.assertEqual(first["retry_later"], 1)
        self.assertEqual(db.get_status("m1"), db.STATUS_PENDING)

        second = self.processor.poll_once()

        self.assertEqual(second["draft_created"], 1)
        self.assertEqual(len(self.service.created_drafts), 1)

    def test_reply_generation_error_is_retried(self):
        self.add_message("m1")
        self.service.set_history(None, history_page(["m1"], "110"))
        self.generator.errors.append(RuntimeError("model unavailable"))

        first = self.processor.poll_once()
        second = self.processor.poll_once()

        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["draft_created"], 1)
        self.assertEqual(len(self.service.created_drafts), 1)

    def test_repeated_failures_stop_after_max_attempts(self):
        self.add_message("m1")
        self.service.set_history(None, history_page(["m1"], "110"))
        self.generator.errors = [
            RuntimeError("fail") for _ in range(db.MAX_ATTEMPTS + 2)
        ]

        for _ in range(db.MAX_ATTEMPTS + 2):
            self.processor.poll_once()

        self.assertEqual(len(self.generator.inputs), db.MAX_ATTEMPTS)
        self.assertEqual(db.get_status("m1"), db.STATUS_FAILED_PERMANENT)
        self.assertEqual(self.service.created_drafts, [])

    def test_one_failure_does_not_block_other_messages(self):
        self.add_message("bad")
        self.add_message("good")
        self.service.set_history(None, history_page(["bad", "good"], "110"))
        self.generator.errors.append(ValueError("bad reply"))

        summary = self.processor.poll_once()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["draft_created"], 1)

    def test_refresh_error_propagates(self):
        self.add_message("m1")
        self.service.set_history(None, history_page(["m1"], "110"))
        self.service.draft_errors.push(RefreshError("token revoked"))

        with self.assertRaises(RefreshError):
            self.processor.poll_once()

        self.assertEqual(db.get_status("m1"), db.STATUS_PENDING)

    def test_error_description_has_no_email_content(self):
        error = RuntimeError("secret body text from the email")

        self.assertEqual(email_processor.describe_error(error), "RuntimeError")
        self.assertEqual(
            email_processor.describe_error(http_error(429)),
            "HttpError 429",
        )


if __name__ == "__main__":
    unittest.main()
