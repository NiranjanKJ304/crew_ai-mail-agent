import unittest

from googleapiclient.errors import HttpError

from fakes import FakeGmailService, b64url, history_page, http_error, make_message
from src.email_service import gmail_reader
from src.email_service.gmail_reader import (
    HistoryExpiredError,
    decode_base64url,
    extract_body,
    get_new_inbox_message_ids,
    list_inbox_message_ids_since,
    parse_message,
)


class ParseMessageTests(unittest.TestCase):
    def test_parses_headers_and_plain_body(self):
        message = make_message(
            "m1",
            thread_id="t1",
            sender='"Asha K" <Asha@Example.com>',
            to="Agent <agent@college.edu>, other@college.edu",
            subject="  Lab schedule ",
            body="Hi,\r\nIs the lab open on Friday?",
            extra_headers={"Reply-To": "Office <office@example.com>"},
        )

        email = parse_message(message)

        self.assertEqual(email["id"], "m1")
        self.assertEqual(email["thread_id"], "t1")
        self.assertEqual(email["sender_name"], "Asha K")
        self.assertEqual(email["sender_email"], "asha@example.com")
        self.assertEqual(email["reply_to_email"], "office@example.com")
        self.assertEqual(
            email["recipients"],
            ["agent@college.edu", "other@college.edu"],
        )
        self.assertEqual(email["subject"], "Lab schedule")
        self.assertEqual(email["body"], "Hi,\nIs the lab open on Friday?")
        self.assertEqual(email["message_id_header"], "<m1@mail.example.com>")
        self.assertEqual(email["timestamp"], "2026-01-01T00:00:00+00:00")

    def test_missing_subject_and_sender(self):
        message = make_message("m2", subject=None, sender="")

        email = parse_message(message)

        self.assertEqual(email["subject"], "")
        self.assertEqual(email["sender_email"], "")

    def test_completely_malformed_message_does_not_raise(self):
        email = parse_message({})

        self.assertEqual(email["id"], "")
        self.assertEqual(email["body"], "")
        self.assertEqual(email["subject"], "")
        self.assertEqual(email["label_ids"], [])
        self.assertEqual(email["timestamp"], "")

    def test_timestamp_falls_back_to_date_header(self):
        message = make_message("m3", internal_date=None)

        email = parse_message(message)

        self.assertEqual(email["timestamp"], "2026-01-01T00:00:00+00:00")

    def test_header_names_are_case_insensitive(self):
        message = make_message("m4")
        message["payload"]["headers"] = [
            {"name": "SUBJECT", "value": "Upper"},
            {"name": "from", "value": "x@example.com"},
        ]

        email = parse_message(message)

        self.assertEqual(email["subject"], "Upper")
        self.assertEqual(email["sender_email"], "x@example.com")

    def test_body_is_truncated(self):
        message = make_message("m5", body="a" * 50_000)

        email = parse_message(message)

        self.assertEqual(len(email["body"]), gmail_reader.MAX_BODY_CHARS)


class ExtractBodyTests(unittest.TestCase):
    def test_multipart_alternative_prefers_plain_text(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64url("Plain")}},
                {"mimeType": "text/html", "body": {"data": b64url("<b>Html</b>")}},
            ],
        }

        self.assertEqual(extract_body(payload), "Plain")

    def test_html_only_is_converted_to_text(self):
        html = (
            "<html><head><style>p{color:red}</style>"
            "<script>alert('x')</script></head>"
            "<body><p>Hello &amp; welcome</p><p>Line two</p></body></html>"
        )
        payload = {"mimeType": "text/html", "body": {"data": b64url(html)}}

        body = extract_body(payload)

        self.assertIn("Hello & welcome", body)
        self.assertIn("Line two", body)
        self.assertNotIn("alert", body)
        self.assertNotIn("color:red", body)

    def test_nested_multipart_ignores_attachments(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": b64url("Main text")},
                        },
                    ],
                },
                {
                    "mimeType": "text/plain",
                    "filename": "notes.txt",
                    "body": {"attachmentId": "att1"},
                },
                {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Content-Disposition",
                         "value": "attachment; filename=x.txt"},
                    ],
                    "body": {"data": b64url("Attachment text")},
                },
            ],
        }

        self.assertEqual(extract_body(payload), "Main text")

    def test_charset_from_content_type(self):
        payload = {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Content-Type",
                 "value": 'text/plain; charset="iso-8859-1"'},
            ],
            "body": {"data": b64url("Café", charset="iso-8859-1")},
        }

        self.assertEqual(extract_body(payload), "Café")

    def test_empty_body(self):
        payload = {"mimeType": "text/plain", "body": {"size": 0}}

        self.assertEqual(extract_body(payload), "")

    def test_multipart_without_parts(self):
        self.assertEqual(extract_body({"mimeType": "multipart/mixed"}), "")


class DecodeBase64UrlTests(unittest.TestCase):
    def test_decodes_unpadded_urlsafe_unicode(self):
        text = "Hello ✓ — ünïcode ?>?>"
        self.assertEqual(decode_base64url(b64url(text)), text)

    def test_malformed_data_returns_empty_string(self):
        self.assertEqual(decode_base64url("!!!not base64!!!"), "")

    def test_unknown_charset_falls_back_to_utf8(self):
        self.assertEqual(decode_base64url(b64url("ok"), "x-unknown"), "ok")

    def test_none_returns_empty_string(self):
        self.assertEqual(decode_base64url(None), "")


class HistoryTests(unittest.TestCase):
    def test_follows_pagination_and_deduplicates(self):
        service = FakeGmailService()
        service.set_history(None, history_page(["a", "b"], "110", "p2"))
        service.set_history("p2", history_page(["b", "c"], "120"))

        message_ids, latest = get_new_inbox_message_ids(service, "100")

        self.assertEqual(message_ids, ["a", "b", "c"])
        self.assertEqual(latest, "120")
        self.assertEqual(len(service.history_calls), 2)
        self.assertEqual(service.history_calls[0]["startHistoryId"], "100")
        self.assertEqual(service.history_calls[0]["labelId"], "INBOX")
        self.assertEqual(service.history_calls[1]["pageToken"], "p2")

    def test_ignores_messages_not_in_inbox(self):
        service = FakeGmailService()
        service.set_history(
            None,
            history_page(["sent1"], "105", labels=("SENT",)),
        )

        message_ids, latest = get_new_inbox_message_ids(service, "100")

        self.assertEqual(message_ids, [])
        self.assertEqual(latest, "105")

    def test_keeps_messages_without_label_ids(self):
        service = FakeGmailService()
        service.set_history(None, {
            "history": [{"messagesAdded": [{"message": {"id": "x"}}]}],
            "historyId": "101",
        })

        message_ids, _ = get_new_inbox_message_ids(service, "100")

        self.assertEqual(message_ids, ["x"])

    def test_no_changes_returns_empty_list(self):
        service = FakeGmailService(history_id="100")

        message_ids, latest = get_new_inbox_message_ids(service, "100")

        self.assertEqual(message_ids, [])
        self.assertEqual(latest, "100")

    def test_expired_history_raises_history_expired(self):
        service = FakeGmailService()
        service.set_history(None, http_error(404))

        with self.assertRaises(HistoryExpiredError):
            get_new_inbox_message_ids(service, "1")

    def test_server_error_propagates(self):
        service = FakeGmailService()
        service.set_history(None, http_error(500))

        with self.assertRaises(HttpError):
            get_new_inbox_message_ids(service, "100")

    def test_list_since_paginates_and_returns_oldest_first(self):
        service = FakeGmailService()
        service.message_list_pages = {
            None: {"messages": [{"id": "new"}, {"id": "mid"}],
                   "nextPageToken": "p2"},
            "p2": {"messages": [{"id": "old"}]},
        }

        message_ids = list_inbox_message_ids_since(service, 1767225600)

        self.assertEqual(message_ids, ["old", "mid", "new"])
        self.assertEqual(
            service.message_list_calls[0]["q"],
            "in:inbox after:1767225600",
        )


if __name__ == "__main__":
    unittest.main()
