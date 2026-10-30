import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "data" / "processed_emails.db"

STATUS_PENDING = "pending"
STATUS_DRAFT_CREATED = "draft_created"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_FAILED_PERMANENT = "failed_permanent"

# Statuses that mean the message must never be processed again.
FINAL_STATUSES = (
    STATUS_DRAFT_CREATED,
    STATUS_SKIPPED,
    STATUS_FAILED_PERMANENT,
)

MAX_ATTEMPTS = 5

# Columns added after the original (message_id, status) table.
_EXTRA_COLUMNS = {
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "draft_id": "TEXT",
    "detail": "TEXT",
    "updated_at": "TEXT",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect():
    """Open a connection, commit on success, and always close it."""
    connection = sqlite3.connect(DB_PATH)

    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            )
        """)

        existing_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(processed_emails)"
            )
        }

        for column, definition in _EXTRA_COLUMNS.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE processed_emails "
                    f"ADD COLUMN {column} {definition}"
                )

        connection.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def is_processed(message_id):
    """True when the message reached a final status and must be skipped."""
    return get_status(message_id) in FINAL_STATUSES


def get_status(message_id):
    with _connect() as connection:
        row = connection.execute(
            "SELECT status FROM processed_emails WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    return row[0] if row else None


def enqueue_message(message_id):
    """Record a newly detected message as pending.

    Returns False if the message is already known (duplicate).
    """
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO processed_emails
            (message_id, status, attempts, updated_at)
            VALUES (?, ?, 0, ?)
            """,
            (message_id, STATUS_PENDING, _now()),
        )

    return cursor.rowcount == 1


def get_pending_message_ids():
    """Messages waiting to be processed or retried, oldest first."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT message_id FROM processed_emails
            WHERE status IN (?, ?)
            ORDER BY rowid
            """,
            (STATUS_PENDING, STATUS_FAILED),
        ).fetchall()

    return [row[0] for row in rows]


def mark_processed(message_id, status=STATUS_DRAFT_CREATED,
                   draft_id=None, detail=None):
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO processed_emails
            (message_id, status, draft_id, detail, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                draft_id = COALESCE(excluded.draft_id, draft_id),
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (message_id, status, draft_id, detail, _now()),
        )


def mark_failed(message_id, detail):
    """Record a failed attempt. Returns the new status: failed (will be
    retried) or failed_permanent after MAX_ATTEMPTS attempts."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT attempts FROM processed_emails WHERE message_id = ?",
            (message_id,),
        ).fetchone()

        attempts = (row[0] if row else 0) + 1
        status = (
            STATUS_FAILED_PERMANENT
            if attempts >= MAX_ATTEMPTS
            else STATUS_FAILED
        )

        connection.execute(
            """
            INSERT INTO processed_emails
            (message_id, status, attempts, detail, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                attempts = excluded.attempts,
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (message_id, status, attempts, detail, _now()),
        )

    return status


def get_state(key, default=None):
    with _connect() as connection:
        row = connection.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (key,),
        ).fetchone()

    return row[0] if row else default


def set_state(key, value):
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO app_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
