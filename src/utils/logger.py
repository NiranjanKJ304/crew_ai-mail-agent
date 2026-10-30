import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
LOG_FILE = BASE_DIR / "logs" / "email_assistant.log"

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def setup_logging(level=logging.INFO):
    """Log to the console and to logs/email_assistant.log.

    Only message IDs, thread IDs, statuses, and error types are logged.
    Email bodies, subjects, addresses, and credentials are never logged.
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console_handler, file_handler]

    # Google API client logs request URLs at INFO level.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def get_logger(name):
    return logging.getLogger(name)
