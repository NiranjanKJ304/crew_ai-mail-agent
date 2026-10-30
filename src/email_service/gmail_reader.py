import base64
import binascii
import re
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from googleapiclient.errors import HttpError

# Retries for 429 / 5xx responses, with exponential backoff.
API_RETRIES = 3

MAX_BODY_CHARS = 20_000


class HistoryExpiredError(Exception):
    """The stored startHistoryId is too old for the Gmail History API."""


def get_authenticated_email(service):
    profile = (
        service.users()
        .getProfile(userId="me")
        .execute(num_retries=API_RETRIES)
    )

    return profile["emailAddress"].strip().lower()


def get_startup_history_id(service):
    profile = (
        service.users()
        .getProfile(userId="me")
        .execute(num_retries=API_RETRIES)
    )

    return profile["historyId"]


def get_new_inbox_message_ids(service, start_history_id):
    """Return (message_ids, latest_history_id) for INBOX messages added
    after start_history_id, following every page of results.

    Raises HistoryExpiredError when Gmail no longer has history for
    start_history_id (HTTP 404).
    """
    message_ids = []
    seen = set()
    latest_history_id = str(start_history_id)
    page_token = None

    while True:
        request_args = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
            "labelId": "INBOX",
        }

        if page_token:
            request_args["pageToken"] = page_token

        try:
            response = (
                service.users()
                .history()
                .list(**request_args)
                .execute(num_retries=API_RETRIES)
            )
        except HttpError as error:
            if error.resp.status == 404:
                raise HistoryExpiredError(str(start_history_id)) from error
            raise

        for history in response.get("history", []):
            for item in history.get("messagesAdded", []):
                message = item.get("message", {})
                message_id = message.get("id")
                label_ids = message.get("labelIds")

                if not message_id or message_id in seen:
                    continue

                # labelIds is normally present; when it is missing the
                # full message is checked later in the processor.
                if label_ids is not None and "INBOX" not in label_ids:
                    continue

                seen.add(message_id)
                message_ids.append(message_id)

        if response.get("historyId"):
            latest_history_id = str(response["historyId"])

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return message_ids, latest_history_id


def list_inbox_message_ids_since(service, since_epoch_seconds):
    """List INBOX message IDs received after a Unix timestamp.

    Used to recover after the history checkpoint has expired.
    """
    message_ids = []
    page_token = None

    while True:
        request_args = {
            "userId": "me",
            "q": f"in:inbox after:{int(since_epoch_seconds)}",
        }

        if page_token:
            request_args["pageToken"] = page_token

        response = (
            service.users()
            .messages()
            .list(**request_args)
            .execute(num_retries=API_RETRIES)
        )

        for message in response.get("messages", []):
            if message.get("id") and message["id"] not in message_ids:
                message_ids.append(message["id"])

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    # Gmail lists newest first; process oldest first.
    message_ids.reverse()
    return message_ids


def get_email_content(service, message_id):
    message = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="full",
        )
        .execute(num_retries=API_RETRIES)
    )

    return message


def get_email(service, message_id):
    """Fetch a message and return it parsed by parse_message."""
    return parse_message(get_email_content(service, message_id))


def parse_message(message):
    """Convert a Gmail API message resource into a plain dict.

    Missing fields become empty strings, so malformed messages never raise.
    """
    payload = message.get("payload") or {}
    headers = _headers_to_dict(payload.get("headers") or [])

    sender_name, sender_email = parseaddr(headers.get("from", ""))
    reply_to_name, reply_to_email = parseaddr(headers.get("reply-to", ""))

    recipients = [
        address.lower()
        for _, address in getaddresses([headers.get("to", "")])
        if address
    ]

    return {
        "id": message.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "label_ids": message.get("labelIds") or [],
        "sender_name": sender_name,
        "sender_email": sender_email.strip().lower(),
        "reply_to_email": reply_to_email.strip().lower(),
        "recipients": recipients,
        "subject": headers.get("subject", "").strip(),
        "timestamp": _parse_timestamp(message, headers),
        "message_id_header": headers.get("message-id", "").strip(),
        "references": headers.get("references", "").strip(),
        "auto_submitted": headers.get("auto-submitted", "").strip().lower(),
        "precedence": headers.get("precedence", "").strip().lower(),
        "body": extract_body(payload)[:MAX_BODY_CHARS],
    }


def extract_body(payload):
    """Return the decoded text of a MIME payload.

    Prefers text/plain; falls back to text/html converted to text.
    Attachments are ignored.
    """
    plain_parts = []
    html_parts = []

    _collect_text_parts(payload, plain_parts, html_parts)

    if any(part.strip() for part in plain_parts):
        body = "\n".join(plain_parts)
    elif html_parts:
        body = "\n".join(_html_to_text(part) for part in html_parts)
    else:
        body = ""

    body = body.replace("\r\n", "\n")
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def decode_base64url(data, charset="utf-8"):
    """Decode Gmail's base64url body data. Returns "" if it is malformed."""
    if not data:
        return ""

    padded = data + "=" * (-len(data) % 4)

    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return ""

    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _collect_text_parts(part, plain_parts, html_parts):
    mime_type = (part.get("mimeType") or "").lower()
    headers = _headers_to_dict(part.get("headers") or [])
    disposition = headers.get("content-disposition", "").lower()

    if part.get("filename") or disposition.startswith("attachment"):
        return

    if mime_type.startswith("multipart/"):
        for child in part.get("parts") or []:
            _collect_text_parts(child, plain_parts, html_parts)
        return

    data = (part.get("body") or {}).get("data")
    charset = _charset_from_content_type(headers.get("content-type", ""))

    if mime_type == "text/plain":
        plain_parts.append(decode_base64url(data, charset))
    elif mime_type == "text/html":
        html_parts.append(decode_base64url(data, charset))
    elif not mime_type and data:
        plain_parts.append(decode_base64url(data, charset))


def _charset_from_content_type(content_type):
    if not content_type:
        return "utf-8"

    message = Message()
    message["Content-Type"] = content_type
    return message.get_content_charset() or "utf-8"


def _headers_to_dict(headers):
    result = {}

    for header in headers:
        name = (header.get("name") or "").lower()

        # Keep the first occurrence, as mail clients do.
        if name and name not in result:
            result[name] = header.get("value") or ""

    return result


def _parse_timestamp(message, headers):
    internal_date = message.get("internalDate")

    if internal_date:
        try:
            return datetime.fromtimestamp(
                int(internal_date) / 1000,
                tz=timezone.utc,
            ).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass

    try:
        return parsedate_to_datetime(headers.get("date", "")).isoformat()
    except (TypeError, ValueError, IndexError):
        return ""


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.chunks.append(data)


def _html_to_text(html):
    parser = _TextExtractor()

    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)

    text = "".join(parser.chunks)
    return re.sub(r"[ \t]+", " ", text)
