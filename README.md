# CrewAI Email Assistant

An AI email assistant that watches a Gmail inbox and writes reply **drafts** for new emails, using three [CrewAI](https://www.crewai.com/) agents running on [Groq](https://groq.com/).

> **Drafts only.** The assistant never sends or forwards email. Every reply is saved as a Gmail draft, and you review, edit, and send it yourself.

## Features

- **Watches for new mail** with the Gmail History API. It checks every 60 seconds (you can change this).
- **Ignores old mail.** Emails already in the inbox when you first start the assistant are never processed.
- **Survives restarts.** It saves its place, so email that arrives while it is stopped is still handled, and no email gets two drafts.
- **Three AI agents:** one analyzes the email, one writes a reply, one reviews the reply for accuracy, relevance, tone, and grammar.
- **Does not invent facts.** When an answer needs information the email does not contain, the reply gets a placeholder such as `[confirm meeting time]` for you to fill in.
- **Resists prompt injection.** Email content is treated as untrusted data, never as instructions.
- **Threaded replies.** Each draft appears in the original conversation with the correct recipient, subject, and reply headers.
- **Skips mail that should not be answered:** your own sent mail, spam, auto-replies, newsletters, and no-reply senders.
- **Recovers from errors.** Temporary Gmail and AI errors are retried, and the assistant keeps running.
- **Private logs.** Logs contain only message IDs and statuses, never email content or credentials.

## How it works

```
New email arrives in the inbox
        │
        ▼
1. Detect   Gmail History API finds new message IDs since the last check
2. Queue    IDs are saved to a local SQLite database as "pending"
3. Fetch    The email is downloaded and its text decoded
4. Filter   Self-sent, spam, auto-replies, bulk, and no-reply mail is skipped
5. Think    Analyzer → Writer → Reviewer (CrewAI agents on Groq)
6. Draft    The reply is saved as a Gmail draft in the same thread
7. Record   The email is marked as done so it is never handled twice
        │
        ▼
You open Gmail, review the draft, and press Send
```

The AI agents work in sequence. Each one sees the original email, so the reviewer can check the draft against the source:

| Agent | Job |
|---|---|
| Email Intent Analyzer | Finds the sender's intent, questions, missing information, and anything suspicious |
| Reply Writer | Writes a polite reply using only facts from the email, with placeholders for anything unknown |
| Reply Reviewer | Removes unsupported claims, fixes tone and grammar, and returns only the final reply text |

## Project structure

```
crewai_email_assistant/
├── main.py                        # Entry point: startup and monitoring loop
├── config/
│   ├── settings.py                # Reads settings from .env
│   └── agents.yaml                # Agent roles, goals, and backstories
├── src/
│   ├── agents/                    # Analyzer, writer, and reviewer agents
│   ├── tasks/email_tasks.py       # Instructions for each agent
│   ├── crew/email_crew.py         # Builds the crew and Groq model
│   ├── email_service/
│   │   ├── gmail_auth.py          # Gmail sign-in (OAuth)
│   │   ├── gmail_reader.py        # History polling and email decoding
│   │   └── gmail_sender.py        # Draft creation (no send function)
│   ├── services/
│   │   ├── email_processor.py     # Runs the full detect → draft workflow
│   │   └── duplicate_checker.py   # SQLite records and saved checkpoint
│   └── utils/logger.py            # Console and file logging
├── tests/                         # Unit tests (fake Gmail; no real mailbox)
├── credentials/                   # OAuth files (git-ignored)
├── data/                          # SQLite database (git-ignored)
└── logs/                          # Log files (git-ignored)
```

## Requirements

- Python 3.11 or newer
- A Gmail or Google Workspace account for the assistant to watch
- A [Groq API key](https://console.groq.com/keys)
- A Google Cloud project with the Gmail API enabled

## Setup

### 1. Clone and install

```bash
git clone https://github.com/NiranjanKJ304/crew_ai-mail-agent.git
cd crew_ai-mail-agent

python -m venv .venv
```

Activate the virtual environment:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

### 2. Create Google OAuth credentials

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Go to **APIs & Services → Library**, search for **Gmail API**, and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen** and configure it. If the app is in testing mode, add the Gmail account you want to watch as a **test user**.
4. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**, and choose **Desktop app**.
5. Download the JSON file and save it as:

   ```
   credentials/client_secret.json
   ```

The assistant requests these two scopes:

| Scope | Used for |
|---|---|
| `gmail.readonly` | Reading new emails and mailbox history |
| `gmail.compose` | Creating reply drafts |

### 3. Configure `.env`

Copy the example file:

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Then edit `.env`:

```env
groqkey = your-groq-api-key
groqmodel = openai/gpt-oss-120b

agentmail = the-inbox-to-watch@example.com
clientmail = your-personal-email@example.com

autoreply = false
autoforward = false
pollinterval = 60
```

| Variable | Description |
|---|---|
| `groqkey` | Your Groq API key (required) |
| `groqmodel` | Groq model name. Defaults to `openai/gpt-oss-120b` |
| `agentmail` | The Gmail account the assistant watches. You must sign in as this account. |
| `clientmail` | Your personal email address (required) |
| `autoreply`, `autoforward` | Keep these `false`. The assistant never sends or forwards; setting them to `true` only logs a warning. |
| `pollinterval` | Seconds between inbox checks (at least 10, default 60) |

### 4. First run and sign-in

```bash
python main.py
```

On the first run, a browser window opens. Sign in with the `agentmail` account and allow access. The token is saved to `credentials/college_token.json`, so you only sign in once.

When it starts you will see:

```
CrewAI Email Assistant started.
Authenticated account: the-inbox-to-watch@example.com
Monitoring from Gmail history ID: 123456
Existing emails will not be processed by this startup checkpoint.
Automatic sending is disabled. Replies are saved as Gmail drafts only.
Checking for Gmail changes every 60 seconds.
Press Ctrl+C to stop.
```

Press **Ctrl+C** to stop.

## Try it out

1. Start the assistant with `python main.py`.
2. From another email account, send a question to the watched inbox.
3. Within about a minute, the log shows:

   ```
   New email detected: message_id=...
   Generating reply for message_id=...
   Draft created for message_id=... draft_id=... (not sent)
   ```

4. Open Gmail for the watched account and go to the email's conversation. You will find a reply draft addressed to the sender, with subject `Re: <original subject>`.
5. Fill in any `[placeholders]`, edit as needed, and press **Send** yourself.

## Running the tests

The unit tests use a fake Gmail service and never touch a real mailbox.

```bash
python -m unittest discover -s tests -v
```

The tests cover email parsing, history pagination, expired history recovery, duplicate prevention, retries, skip rules, reply generation, draft creation, and a check that no code calls a send method.

The project root also has three manual connection checks. These use your real credentials:

| Script | Checks |
|---|---|
| `test_groq.py` | Groq API key and model |
| `test_crewai_groq.py` | CrewAI connecting to Groq |
| `test_gmail_connection.py` | Gmail sign-in and profile access |

## Reliability details

| Situation | What happens |
|---|---|
| App restarted | Resumes from the saved checkpoint. Emails that already have drafts are not processed again. |
| Crash after a draft was created | Before creating a draft, the assistant checks the thread for an existing one, so no duplicate is made. |
| Gmail history too old (about a week) | Recovers by searching recent inbox mail since the last check (at most 7 days). |
| Gmail rate limit or server error | Retried on the next check. |
| AI error | Retried up to 5 times, then marked as failed. |
| Gmail token expired or revoked | The app stops with a message. Delete `credentials/college_token.json` and run it again to sign in. |

Processing records and the checkpoint are stored in `data/processed_emails.db`. Logs are written to `logs/email_assistant.log`.

## Troubleshooting

**`PermissionError: Expected ..., but authenticated as ...`**
You signed in with a different account from `agentmail`. Delete `credentials/college_token.json` and run again, signing in with the correct account.

**`Error 403: access_denied` during sign-in**
Add the account as a test user on the OAuth consent screen in Google Cloud Console.

**No draft appears**
Check `logs/email_assistant.log`. The email may have been skipped (for example, it came from the watched account itself, was an auto-reply, or came from a no-reply address), or it may still be retrying after an error.

**Garbled characters in the Windows console**
The app switches the console to UTF-8 automatically. If you run other scripts directly, set `PYTHONIOENCODING=utf-8`.

## Security

- `.env`, `credentials/`, `data/`, and `logs/` are excluded by `.gitignore`. Never commit them.
- If `client_secret.json` or a token is ever exposed, revoke it in Google Cloud Console and create a new one.
- CrewAI's verbose console output shows email content while the agents work. It is printed to the terminal only, not written to the log file. Set `verbose=False` in the files in `src/agents/` to turn it off.
- AI-generated replies can still contain mistakes. Always read a draft before sending it.
