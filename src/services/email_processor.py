"""Detect new Gmail messages and turn them into reply drafts.

Flow for each poll:
1. Read Gmail history after the saved checkpoint (all pages).
2. Queue every new INBOX message ID in SQLite as "pending".
3. Only then save the new checkpoint, so a crash never loses a message.
4. Process queued messages: fetch, filter, generate a reply, create a draft.

Emails are never sent or forwarded; only drafts are created.
"""

import time

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from src.crew.email_crew import format_email_for_crew, generate_reply
from src.email_service.gmail_reader import (
    HistoryExpiredError,
    get_email,
    get_new_inbox_message_ids,
    get_startup_history_id,
    list_inbox_message_ids_since,
)
from src.email_service.gmail_sender import (
    create_reply_draft,
    find_existing_reply_draft,
    get_reply_address,
)
from src.services import duplicate_checker as db
from src.utils.logger import get_logger

logger = get_logger(__name__)

STATE_HISTORY_ID = "history_id"
STATE_ACCOUNT = "account_email"
STATE_LAST_SYNC_AT = "last_sync_at"
STATE_STARTED_AT = "monitoring_started_at"

# When the history checkpoint has expired, look back at most this far.
MAX_RECOVERY_SECONDS = 7 * 24 * 60 * 60
# Overlap with the last sync so nothing on the boundary is missed.
RECOVERY_OVERLAP_SECONDS = 5 * 60

NO_REPLY_PREFIXES = (
    "noreply",
    "no-reply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
)

BULK_PRECEDENCE = ("bulk", "junk", "list")

# Outcomes returned by process_message.
OUTCOME_DRAFT_CREATED = "draft_created"
OUTCOME_ALREADY_HANDLED = "already_handled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_RETRY_LATER = "retry_later"


def is_transient_error(error):
    """Errors worth retrying later without counting as a failed attempt."""
    if isinstance(error, HttpError):
        return error.resp.status == 429 or error.resp.status >= 500

    return isinstance(
        error,
        (TransportError, httplib2.HttpLib2Error, OSError),
    )


def describe_error(error):
    """Short error description that never includes email content."""
    if isinstance(error, HttpError):
        return f"HttpError {error.resp.status}"

    return type(error).__name__


class EmailProcessor:
    def __init__(self, service, account_email,
                 reply_generator=generate_reply, clock=time.time):
        self.service = service
        self.account_email = account_email.strip().lower()
        self.reply_generator = reply_generator
        self.clock = clock

    # ----- checkpoint -------------------------------------------------

    def initialize_checkpoint(self):
        """Resume from the saved checkpoint, or start from "now" so that
        emails already in the mailbox are never processed.

        Returns (history_id, resumed).
        """
        saved_history_id = db.get_state(STATE_HISTORY_ID)
        saved_account = db.get_state(STATE_ACCOUNT)

        if saved_history_id and saved_account == self.account_email:
            return saved_history_id, True

        history_id = str(get_startup_history_id(self.service))
        now = int(self.clock())

        db.set_state(STATE_HISTORY_ID, history_id)
        db.set_state(STATE_ACCOUNT, self.account_email)
        db.set_state(STATE_LAST_SYNC_AT, now)
        db.set_state(STATE_STARTED_AT, now)

        return history_id, False

    def sync_history(self):
        """Queue new INBOX messages and advance the checkpoint.

        Returns (newly_queued, duplicates). Gmail API errors propagate and
        leave the checkpoint unchanged.
        """
        history_id = db.get_state(STATE_HISTORY_ID)

        try:
            message_ids, latest_history_id = get_new_inbox_message_ids(
                self.service,
                history_id,
            )
        except HistoryExpiredError:
            logger.warning(
                "History checkpoint %s has expired; recovering from "
                "recent inbox messages.",
                history_id,
            )
            # Read the new checkpoint first so nothing arriving during
            # recovery falls into a gap.
            latest_history_id = str(get_startup_history_id(self.service))
            message_ids = list_inbox_message_ids_since(
                self.service,
                self._recovery_start_time(),
            )

        newly_queued = 0
        duplicates = 0

        for message_id in message_ids:
            if db.enqueue_message(message_id):
                newly_queued += 1
                logger.info("New email detected: message_id=%s", message_id)
            else:
                duplicates += 1
                logger.info(
                    "Duplicate notification ignored: message_id=%s",
                    message_id,
                )

        # Every message ID is stored before the checkpoint moves.
        db.set_state(STATE_HISTORY_ID, latest_history_id)
        db.set_state(STATE_LAST_SYNC_AT, int(self.clock()))

        return newly_queued, duplicates

    def _recovery_start_time(self):
        now = int(self.clock())
        last_sync = int(db.get_state(STATE_LAST_SYNC_AT, now))
        started_at = int(db.get_state(STATE_STARTED_AT, last_sync))

        return max(
            last_sync - RECOVERY_OVERLAP_SECONDS,
            started_at,
            now - MAX_RECOVERY_SECONDS,
        )

    # ----- processing -------------------------------------------------

    def poll_once(self):
        """Run one monitoring cycle. Returns a summary dict."""
        newly_queued, duplicates = self.sync_history()

        summary = {
            "detected": newly_queued,
            "duplicates": duplicates,
            OUTCOME_DRAFT_CREATED: 0,
            OUTCOME_ALREADY_HANDLED: 0,
            OUTCOME_SKIPPED: 0,
            OUTCOME_FAILED: 0,
            OUTCOME_RETRY_LATER: 0,
        }

        for message_id in db.get_pending_message_ids():
            outcome = self.process_message(message_id)
            summary[outcome] += 1

        return summary

    def process_message(self, message_id):
        if db.is_processed(message_id):
            return OUTCOME_ALREADY_HANDLED

        try:
            email = get_email(self.service, message_id)
        except HttpError as error:
            if error.resp.status == 404:
                db.mark_processed(
                    message_id,
                    db.STATUS_SKIPPED,
                    detail="not_found",
                )
                logger.info(
                    "Skipped message_id=%s: no longer exists",
                    message_id,
                )
                return OUTCOME_SKIPPED
            return self._handle_failure(message_id, error)
        except Exception as error:
            return self._handle_failure(message_id, error)

        skip_reason = self.get_skip_reason(email)

        if skip_reason:
            db.mark_processed(
                message_id,
                db.STATUS_SKIPPED,
                detail=skip_reason,
            )
            logger.info("Skipped message_id=%s: %s", message_id, skip_reason)
            return OUTCOME_SKIPPED

        try:
            existing_draft = find_existing_reply_draft(self.service, email)

            if existing_draft:
                db.mark_processed(
                    message_id,
                    db.STATUS_DRAFT_CREATED,
                    draft_id=existing_draft,
                    detail="existing_draft_found",
                )
                logger.info(
                    "Reply draft already exists for message_id=%s; "
                    "not creating another.",
                    message_id,
                )
                return OUTCOME_ALREADY_HANDLED

            logger.info(
                "Generating reply for message_id=%s thread_id=%s",
                message_id,
                email["thread_id"],
            )
            reply = self.reply_generator(format_email_for_crew(email))

            draft_id = create_reply_draft(
                self.service,
                email,
                reply,
                self.account_email,
            )
        except Exception as error:
            return self._handle_failure(message_id, error)

        db.mark_processed(
            message_id,
            db.STATUS_DRAFT_CREATED,
            draft_id=draft_id,
        )
        logger.info(
            "Draft created for message_id=%s draft_id=%s (not sent)",
            message_id,
            draft_id,
        )
        return OUTCOME_DRAFT_CREATED

    def get_skip_reason(self, email):
        """Return why an email must not get a reply draft, or None."""
        labels = set(email.get("label_ids") or [])
        sender = email.get("sender_email", "")
        reply_address = get_reply_address(email)

        if "SENT" in labels or "DRAFT" in labels:
            return "sent_by_authenticated_account"

        if sender == self.account_email:
            return "sent_by_authenticated_account"

        if "SPAM" in labels or "TRASH" in labels:
            return "spam_or_trash"

        if labels and "INBOX" not in labels:
            return "not_in_inbox"

        if not reply_address:
            return "missing_sender"

        if reply_address == self.account_email:
            return "reply_address_is_authenticated_account"

        if email.get("auto_submitted", "") not in ("", "no"):
            return "auto_submitted"

        if email.get("precedence", "") in BULK_PRECEDENCE:
            return "bulk_mail"

        local_part = reply_address.split("@", 1)[0]

        if local_part.startswith(NO_REPLY_PREFIXES):
            return "no_reply_sender"

        return None

    def _handle_failure(self, message_id, error):
        # An expired or revoked token affects every message; let main stop.
        if isinstance(error, RefreshError):
            raise error

        detail = describe_error(error)

        if is_transient_error(error):
            logger.warning(
                "Temporary error for message_id=%s (%s); will retry "
                "on the next poll.",
                message_id,
                detail,
            )
            return OUTCOME_RETRY_LATER

        status = db.mark_failed(message_id, detail)

        if status == db.STATUS_FAILED_PERMANENT:
            logger.error(
                "Giving up on message_id=%s after %s attempts (%s)",
                message_id,
                db.MAX_ATTEMPTS,
                detail,
            )
        else:
            logger.warning(
                "Failed to process message_id=%s (%s); will retry.",
                message_id,
                detail,
            )

        return OUTCOME_FAILED
