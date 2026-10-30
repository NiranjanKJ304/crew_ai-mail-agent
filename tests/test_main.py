import unittest
from unittest import mock

from google.auth.exceptions import RefreshError

import main
from fakes import http_error

EMPTY_SUMMARY = {
    "detected": 0,
    "duplicates": 0,
    "draft_created": 0,
    "already_handled": 0,
    "skipped": 0,
    "failed": 0,
    "retry_later": 0,
}


class RunCycleTests(unittest.TestCase):
    def test_recoverable_error_is_logged_and_loop_continues(self):
        processor = mock.Mock()
        processor.poll_once.side_effect = http_error(503)

        with self.assertLogs("main", level="ERROR") as logs:
            main.run_cycle(processor)

        self.assertIn("HttpError 503", logs.output[0])

    def test_unexpected_error_does_not_leak_message(self):
        processor = mock.Mock()
        processor.poll_once.side_effect = RuntimeError("private email text")

        with self.assertLogs("main", level="ERROR") as logs:
            main.run_cycle(processor)

        self.assertNotIn("private email text", logs.output[0])

    def test_revoked_token_stops_application(self):
        processor = mock.Mock()
        processor.poll_once.side_effect = RefreshError("revoked")

        with self.assertLogs("main", level="ERROR"):
            with self.assertRaises(SystemExit):
                main.run_cycle(processor)

    def test_logs_cycle_summary(self):
        processor = mock.Mock()
        processor.poll_once.return_value = dict(
            EMPTY_SUMMARY, detected=1, draft_created=1
        )

        with self.assertLogs("main", level="INFO") as logs:
            main.run_cycle(processor)

        self.assertIn("detected=1", logs.output[0])
        self.assertIn("drafts=1", logs.output[0])

    def test_ctrl_c_stops_gracefully(self):
        processor = mock.Mock()
        processor.initialize_checkpoint.return_value = ("100", False)
        service = mock.Mock()

        with mock.patch.multiple(
            main,
            setup_logging=mock.DEFAULT,
            validate_settings=mock.DEFAULT,
            initialize_database=mock.DEFAULT,
            get_gmail_service=mock.Mock(return_value=service),
            get_authenticated_email=mock.Mock(
                return_value=main.COLLEGE_EMAIL
            ),
            EmailProcessor=mock.Mock(return_value=processor),
            run_cycle=mock.Mock(side_effect=KeyboardInterrupt),
        ), mock.patch("builtins.print") as printed:
            main.main()

        printed_text = " ".join(
            str(call.args[0]) for call in printed.call_args_list if call.args
        )
        self.assertIn("Automatic sending is disabled", printed_text)
        self.assertIn("stopped by user", printed_text)


if __name__ == "__main__":
    unittest.main()
