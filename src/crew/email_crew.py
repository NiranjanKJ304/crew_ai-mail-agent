import re
from pathlib import Path

import yaml

from crewai import Crew, LLM, Process

from config.settings import GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL
from src.agents.email_analyzer import create_email_analyzer
from src.agents.reply_writer import create_reply_writer
from src.agents.reply_reviewer import create_reply_reviewer
from src.tasks.email_tasks import EMAIL_END, EMAIL_START, create_email_tasks

BASE_DIR = Path(__file__).resolve().parents[2]
AGENTS_CONFIG = BASE_DIR / "config" / "agents.yaml"


def create_llm():
    # Same setup as test_crewai_groq.py: Groq's OpenAI-compatible API.
    # custom_openai strips one "openai/" prefix, so Groq receives
    # GROQ_MODEL unchanged (for example "openai/gpt-oss-120b").
    # A LiteLLM-style "groq/" prefix is not part of Groq's model name.
    model_name = GROQ_MODEL.removeprefix("groq/")

    return LLM(
        model=f"openai/{model_name}",
        custom_openai=True,
        base_url=GROQ_BASE_URL,
        api_key=GROQ_API_KEY,
        temperature=0.2,
    )


def create_email_crew():
    with open(AGENTS_CONFIG, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    llm = create_llm()

    analyzer = create_email_analyzer(
        llm, config["email_analyzer"]
    )

    writer = create_reply_writer(
        llm, config["reply_writer"]
    )

    reviewer = create_reply_reviewer(
        llm, config["reply_reviewer"]
    )

    tasks = create_email_tasks(analyzer, writer, reviewer)

    return Crew(
        agents=[analyzer, writer, reviewer],
        tasks=list(tasks),
        process=Process.sequential,
        verbose=True,
    )


def format_email_for_crew(email):
    """Turn a parsed email into the text given to the agents.

    Removes the delimiter markers and template placeholders from the
    untrusted content so it cannot break out of its block.
    """
    body = email.get("body") or "(empty body)"

    content = (
        f"From: {email.get('sender_name', '')} "
        f"<{email.get('sender_email', '')}>\n"
        f"Subject: {email.get('subject') or '(no subject)'}\n"
        f"Date: {email.get('timestamp', '')}\n"
        f"\n{body}"
    )

    for marker in (EMAIL_START, EMAIL_END, "{email_content}"):
        content = content.replace(marker, "")

    return content


def clean_reply(text):
    text = (text or "").strip()

    fenced = re.fullmatch(r"```[a-zA-Z]*\n(.*?)\n?```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # Drop a leading "Subject: ..." line if the model added one.
    text = re.sub(r"\A\s*subject:[^\n]*\n+", "", text, flags=re.IGNORECASE)

    return text.strip()


def generate_reply(email_content: str) -> str:
    crew = create_email_crew()

    result = crew.kickoff(
        inputs={"email_content": email_content}
    )

    reply = clean_reply(getattr(result, "raw", None) or str(result))

    if not reply:
        raise ValueError("Crew returned an empty reply")

    return reply
