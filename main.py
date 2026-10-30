import sys
import time

from google.auth.exceptions import RefreshError

from config.settings import (
    AUTO_FORWARD_ENABLED,
    AUTO_REPLY_ENABLED,
    COLLEGE_EMAIL,
    POLL_INTERVAL_SECONDS,
    validate_settings,
)

from src.email_service.gmail_auth import get_gmail_service
from src.email_service.gmail_reader import get_authenticated_email
from src.services.duplicate_checker import initialize_database
from src.services.email_processor import EmailProcessor, describe_error
from src.utils.logger import get_logger, setup_logging

logger = get_logger("main")


def main():
    # CrewAI's console output uses Unicode box characters that the
    # default Windows console encoding cannot print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    setup_logging()
    validate_settings()
    initialize_database()

    service = get_gmail_service()
    authenticated_email = get_authenticated_email(service)

    if authenticated_email != COLLEGE_EMAIL:
        raise PermissionError(
            f"Expected {COLLEGE_EMAIL}, "
            f"but authenticated as {authenticated_email}"
        )

    processor = EmailProcessor(service, authenticated_email)
    history_id, resumed = processor.initialize_checkpoint()

    print("\nCrewAI Email Assistant started.")
    print(f"Authenticated account: {authenticated_email}")

    if resumed:
        print(f"Resuming from saved Gmail history ID: {history_id}")
        print("Emails received while the app was stopped will be processed.")
    else:
        print(f"Monitoring from Gmail history ID: {history_id}")
        print("Existing emails will not be processed by this startup checkpoint.")

    print("Automatic sending is disabled. Replies are saved as Gmail drafts only.")

    if AUTO_REPLY_ENABLED or AUTO_FORWARD_ENABLED:
        logger.warning(
            "autoreply/autoforward is set to true in .env, but this "
            "application never sends or forwards email. Drafts only."
        )

    print(f"Checking for Gmail changes every {POLL_INTERVAL_SECONDS} seconds.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            run_cycle(processor)
            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nEmail Assistant stopped by user.")


def run_cycle(processor):
    """Run one poll. Recoverable errors are logged and monitoring continues;
    an expired or revoked OAuth token stops the application."""
    try:
        summary = processor.poll_once()
    except RefreshError:
        logger.error(
            "Gmail authorization has expired or was revoked. Delete "
            "credentials/college_token.json and restart to sign in again."
        )
        raise SystemExit(1)
    except Exception as error:
        logger.error(
            "Monitoring cycle failed (%s); retrying in %s seconds. "
            "Unqueued history is not skipped and queued emails are kept.",
            describe_error(error),
            POLL_INTERVAL_SECONDS,
        )
        return

    if any(summary.values()):
        logger.info(
            "Cycle complete: detected=%s duplicates=%s drafts=%s "
            "already_handled=%s skipped=%s failed=%s retry_later=%s",
            summary["detected"],
            summary["duplicates"],
            summary["draft_created"],
            summary["already_handled"],
            summary["skipped"],
            summary["failed"],
            summary["retry_later"],
        )
    else:
        logger.info("Monitoring is active. No new emails.")


if __name__ == "__main__":
    main()
