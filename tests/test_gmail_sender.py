import ast
import base64
import email as email_lib
import unittest
from email import policy
from pathlib import Path

from fakes import FakeGmailService, make_message
from src.email_service.gmail_reader import parse_message
from src.email_service.gmail_sender import (
    build_reply_subject,
    create_reply_draft,
    find_existing_reply_draft,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def decode_raw(draft_body):
    raw = draft_body["message"]["raw"]
    return email_lib.message_from_bytes(
        base64.urlsafe_b64decode(raw),
        policy=policy.default,
    )


class ReplySubjectTests(unittest.TestCase):
    def test_adds_re_prefix(self):
        self.assertEqual(build_reply_subject("Exam dates"), "Re: Exam dates")

    def test_keeps_existing_re_prefix(self):
        self.assertEqual(build_reply_subject("RE: Exam dates"), "RE: Exam dates")

    def test_missing_subject(self):
        self.assertEqual(build_reply_subject(""), "Re: (no subject)")
        self.assertEqual(build_reply_subject(None), "Re: (no subject)")


class CreateReplyDraftTests(unittest.TestCase):
    def test_creates_threaded_draft_with_correct_headers(self):
        service = FakeGmailService()
        email = parse_message(make_message(
            "m1",
            thread_id="t1",
            sender="Asha K <asha@example.com>",
            subject="Project question",
            extra_headers={"References": "<earlier@mail.example.com>"},
        ))

        draft_id = create_reply_draft(
            service,
            email,
            "Thank you for your email.",
            "agent@college.edu",
        )

        self.assertEqual(draft_id, "draft-1")
        self.assertEqual(len(service.created_drafts), 1)
        self.assertEqual(service.send_calls, [])

        draft_body = service.created_drafts[0]
        self.assertEqual(draft_body["message"]["threadId"], "t1")

        mime = decode_raw(draft_body)
        self.assertEqual(mime["From"], "agent@college.edu")
        self.assertEqual(mime["To"], "Asha K <asha@example.com>")
        self.assertEqual(mime["Subject"], "Re: Project question")
        self.assertEqual(mime["In-Reply-To"], "<m1@mail.example.com>")
        self.assertEqual(
            mime["References"],
            "<earlier@mail.example.com> <m1@mail.example.com>",
        )
        self.assertEqual(
            mime.get_content().strip(),
            "Thank you for your email.",
        )

    def test_uses_reply_to_address(self):
        service = FakeGmailService()
        email = parse_message(make_message(
            "m2",
            extra_headers={"Reply-To": "office@example.com"},
        ))

        create_reply_draft(service, email, "Noted.", "agent@college.edu")

        mime = decode_raw(service.created_drafts[0])
        self.assertEqual(mime["To"], "office@example.com")

    def test_missing_message_id_header_still_creates_draft(self):
        service = FakeGmailService()
        message = make_message("m3", subject=None)
        message["payload"]["headers"] = [
            h for h in message["payload"]["headers"]
            if h["name"] != "Message-ID"
        ]
        email = parse_message(message)

        create_reply_draft(service, email, "Hello.", "agent@college.edu")

        mime = decode_raw(service.created_drafts[0])
        self.assertEqual(mime["Subject"], "Re: (no subject)")
        self.assertIsNone(mime["In-Reply-To"])

    def test_missing_sender_raises(self):
        service = FakeGmailService()
        email = parse_message(make_message("m4", sender=""))

        with self.assertRaises(ValueError):
            create_reply_draft(service, email, "Hello.", "agent@college.edu")

        self.assertEqual(service.created_drafts, [])


class FindExistingReplyDraftTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeGmailService()
        self.email = parse_message(make_message("m1", thread_id="t1"))

    def _thread_message(self, message_id, labels, in_reply_to):
        return {
            "id": message_id,
            "labelIds": labels,
            "payload": {
                "headers": [{"name": "In-Reply-To", "value": in_reply_to}],
            },
        }

    def test_finds_matching_draft(self):
        self.service.threads["t1"] = {"messages": [
            self._thread_message("d1", ["DRAFT"], "<m1@mail.example.com>"),
        ]}

        self.assertEqual(
            find_existing_reply_draft(self.service, self.email),
            "d1",
        )

    def test_ignores_sent_replies_and_other_drafts(self):
        self.service.threads["t1"] = {"messages": [
            self._thread_message("s1", ["SENT"], "<m1@mail.example.com>"),
            self._thread_message("d2", ["DRAFT"], "<other@mail.example.com>"),
        ]}

        self.assertIsNone(find_existing_reply_draft(self.service, self.email))

    def test_returns_none_without_message_id(self):
        self.email["message_id_header"] = ""

        self.assertIsNone(find_existing_reply_draft(self.service, self.email))


class NoSendingTests(unittest.TestCase):
    def test_source_never_calls_send(self):
        """No module in src/ or main.py calls a .send(...) method."""
        files = list((PROJECT_ROOT / "src").rglob("*.py"))
        files.append(PROJECT_ROOT / "main.py")

        offenders = []

        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "send"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
