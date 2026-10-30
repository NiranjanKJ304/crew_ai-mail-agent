"""In-memory stand-ins for the Gmail API and the SQLite database.

No test touches a real mailbox: every Gmail call goes to FakeGmailService,
which records drafts created and fails loudly if anything tries to send.
"""

import base64
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import httplib2
from googleapiclient.errors import HttpError

from src.services import duplicate_checker


def b64url(text, charset="utf-8"):
    """Encode like Gmail: base64url without padding."""
    return base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")


def http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


def make_message(message_id, thread_id=None, sender="Asha <asha@example.com>",
                 to="agent@college.edu", subject="Project question",
                 body="Hello, when is the review meeting?",
                 labels=("INBOX", "UNREAD"), extra_headers=None,
                 internal_date="1767225600000"):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Message-ID", "value": f"<{message_id}@mail.example.com>"},
        {"name": "Date", "value": "Thu, 01 Jan 2026 00:00:00 +0000"},
    ]

    if subject is not None:
        headers.append({"name": "Subject", "value": subject})

    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})

    return {
        "id": message_id,
        "threadId": thread_id or f"thread-{message_id}",
        "labelIds": list(labels),
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": b64url(body)},
        },
    }


def history_page(message_ids, history_id, next_page_token=None,
                 labels=("INBOX", "UNREAD")):
    page = {
        "history": [
            {
                "id": history_id,
                "messagesAdded": [
                    {"message": {"id": mid, "labelIds": list(labels)}}
                    for mid in message_ids
                ],
            }
        ],
        "historyId": history_id,
    }

    if next_page_token:
        page["nextPageToken"] = next_page_token

    return page


class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self, num_retries=0):
        result = self._result() if callable(self._result) else self._result

        if isinstance(result, Exception):
            raise result

        return result


class _Queue:
    """Return the next queued response, or a default when empty."""

    def __init__(self, default):
        self.items = []
        self.default = default

    def push(self, *items):
        self.items.extend(items)

    def pop(self):
        return self.items.pop(0) if self.items else self.default


class FakeGmailService:
    def __init__(self, email="agent@college.edu", history_id="100"):
        self.profile = {"emailAddress": email, "historyId": history_id}
        # history.list responses keyed by pageToken (None = first page).
        # Each value is a _Queue, so successive polls can differ.
        self.history_pages = {}
        self.history_calls = []
        self.messages = {}
        self.message_list_pages = {}
        self.message_list_calls = []
        self.threads = {}
        self.draft_errors = _Queue(None)
        self.created_drafts = []
        self.send_calls = []

    def set_history(self, page_token, *responses):
        queue = self.history_pages.setdefault(
            page_token,
            _Queue({"historyId": self.profile["historyId"]}),
        )
        queue.push(*responses)

    def users(self):
        return _Users(self)


class _Users:
    def __init__(self, fake):
        self.fake = fake

    def getProfile(self, userId):
        return _Request(dict(self.fake.profile))

    def history(self):
        return _History(self.fake)

    def messages(self):
        return _Messages(self.fake)

    def threads(self):
        return _Threads(self.fake)

    def drafts(self):
        return _Drafts(self.fake)


class _History:
    def __init__(self, fake):
        self.fake = fake

    def list(self, **kwargs):
        self.fake.history_calls.append(kwargs)
        queue = self.fake.history_pages.get(kwargs.get("pageToken"))

        if queue is None:
            return _Request({"historyId": self.fake.profile["historyId"]})

        return _Request(queue.pop())


class _Messages:
    def __init__(self, fake):
        self.fake = fake

    def get(self, userId, id, format):
        message = self.fake.messages.get(id, http_error(404))
        return _Request(message)

    def list(self, **kwargs):
        self.fake.message_list_calls.append(kwargs)
        return _Request(
            self.fake.message_list_pages.get(kwargs.get("pageToken"), {})
        )

    def send(self, **kwargs):
        self.fake.send_calls.append(kwargs)
        raise AssertionError("messages.send must never be called")


class _Threads:
    def __init__(self, fake):
        self.fake = fake

    def get(self, userId, id, **kwargs):
        return _Request(self.fake.threads.get(id, {"id": id, "messages": []}))


class _Drafts:
    def __init__(self, fake):
        self.fake = fake

    def create(self, userId, body):
        def run():
            error = self.fake.draft_errors.pop()

            if error is not None:
                return error

            self.fake.created_drafts.append(body)
            return {"id": f"draft-{len(self.fake.created_drafts)}"}

        return _Request(run)

    def send(self, **kwargs):
        self.fake.send_calls.append(kwargs)
        raise AssertionError("drafts.send must never be called")


class TempDatabaseMixin:
    """Point duplicate_checker at a fresh SQLite file for each test."""

    def setUp(self):
        super().setUp()
        self._temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self._temp_dir) / "test.db"
        patcher = mock.patch.object(duplicate_checker, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        duplicate_checker.initialize_database()
