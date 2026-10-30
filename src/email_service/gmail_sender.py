"""Create reply drafts in Gmail.

This module only calls users.drafts.create. It never calls messages.send
or drafts.send, so nothing is ever sent automatically. A person must open
the draft in Gmail, review it, and press Send.
"""

import base64
from email.message import EmailMessage
from email.utils import formataddr

from src.email_service.gmail_reader import API_RETRIES


def build_reply_subject(subject):
    subject = (subject or "").strip()

    if not subject:
        return "Re: (no subject)"

    if subject.lower().startswith("re:"):
        return subject

    return f"Re: {subject}"


def get_reply_address(email):
    return email.get("reply_to_email") or email.get("sender_email") or ""


def build_reply_message(email, reply_body, from_address):
    """Build the MIME reply with threading headers."""
    to_address = get_reply_address(email)

    if not to_address:
        raise ValueError("Email has no sender address to reply to")

    message = EmailMessage()
    message["From"] = from_address

    if to_address == email.get("sender_email") and email.get("sender_name"):
        message["To"] = formataddr((email["sender_name"], to_address))
    else:
        message["To"] = to_address

    message["Subject"] = build_reply_subject(email.get("subject"))

    original_id = email.get("message_id_header")

    if original_id:
        message["In-Reply-To"] = original_id
        references = email.get("references", "")
        message["References"] = (
            f"{references} {original_id}".strip()
            if original_id not in references
            else references
        )

    message.set_content(reply_body)
    return message


def create_reply_draft(service, email, reply_body, from_address):
    """Create a reply draft in the original thread. Returns the draft ID."""
    message = build_reply_message(email, reply_body, from_address)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    draft_body = {"message": {"raw": raw}}

    if email.get("thread_id"):
        draft_body["message"]["threadId"] = email["thread_id"]

    draft = (
        service.users()
        .drafts()
        .create(userId="me", body=draft_body)
        .execute(num_retries=API_RETRIES)
    )

    return draft["id"]


def find_existing_reply_draft(service, email):
    """Return the message ID of a draft in the thread that already replies
    to this email, or None.

    Prevents a duplicate draft when the app stopped after creating a draft
    but before recording it in the database.
    """
    original_id = email.get("message_id_header")
    thread_id = email.get("thread_id")

    if not original_id or not thread_id:
        return None

    thread = (
        service.users()
        .threads()
        .get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["In-Reply-To"],
        )
        .execute(num_retries=API_RETRIES)
    )

    for message in thread.get("messages", []):
        if "DRAFT" not in (message.get("labelIds") or []):
            continue

        headers = (message.get("payload") or {}).get("headers") or []

        for header in headers:
            if (
                (header.get("name") or "").lower() == "in-reply-to"
                and (header.get("value") or "").strip() == original_id
            ):
                return message.get("id")

    return None
