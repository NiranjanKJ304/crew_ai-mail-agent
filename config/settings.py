import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("groqkey", "")
# "or" keeps the default when .env has an empty "groqmodel =" line.
GROQ_MODEL = os.getenv("groqmodel") or "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

COLLEGE_EMAIL = os.getenv("agentmail", "").strip().lower()
PERSONAL_EMAIL = os.getenv("clientmail", "").strip().lower()

AUTO_REPLY_ENABLED = (
    os.getenv("autoreply", "false").lower() == "true"
)

AUTO_FORWARD_ENABLED = (
    os.getenv("autoforward", "false").lower() == "true"
)

POLL_INTERVAL_SECONDS = int(
    os.getenv("pollinterval", "60")
)

def validate_settings():
    if not GROQ_API_KEY:
        raise ValueError("groqkey (Groq API key) is missing from .env")

    if not COLLEGE_EMAIL:
        raise ValueError("agentmail (college email) is missing from .env")

    if not PERSONAL_EMAIL:
        raise ValueError("clientmail (personal email) is missing from .env")

    if POLL_INTERVAL_SECONDS < 10:
        raise ValueError("pollinterval must be at least 10 seconds")
