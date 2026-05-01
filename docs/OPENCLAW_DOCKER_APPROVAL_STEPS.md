# OpenClaw Docker Approval Steps

The code in this repository is ready to run without secrets, but these steps require your explicit
approval because they involve tokens, accounts, or external services.

## 1. Telegram Bot

| Step | Why Approval Is Needed |
|---|---|
| Create bot with BotFather | Creates a real Telegram bot identity |
| Add `TELEGRAM_BOT_TOKEN` to `.env` | Gives the service permission to send/poll bot messages |
| Add `TELEGRAM_ALLOWED_CHAT_ID` to `.env` | Restricts delivery to your chat |

## 2. OpenAI API Key

| Step | Why Approval Is Needed |
|---|---|
| Create dedicated OpenAI project/key | Enables paid LLM calls |
| Set project budget/alerts | Provider-side visibility |
| Add `OPENAI_API_KEY` to `.env` | Lets the bot generate source ideas and cover notes |

The bot also enforces its own budget with:

```text
JOBBOT_DAILY_BUDGET_USD=0.50
JOBBOT_MONTHLY_BUDGET_USD=10.00
```

## 3. OpenClaw Gateway In Docker

OpenClaw's Docker docs describe a setup/onboarding flow for the full Gateway. The important points
for this project:

| Requirement | Setting |
|---|---|
| Use prebuilt image | `ghcr.io/openclaw/openclaw:latest` |
| Bind locally | `127.0.0.1:18789:18789` |
| Persist only OpenClaw config/workspace | `./openclaw/config`, `./openclaw/workspace` |
| Do not mount browser cookies | Not needed and unsafe |
| Do not expose publicly | Use localhost or private network only |

After you approve OpenClaw onboarding:

```bash
docker compose --profile openclaw up -d openclaw-gateway
```

Then open:

```text
http://127.0.0.1:18789/
```

## 4. Email Alerts

For LinkedIn job alerts, use email parsing only.

Recommended pattern:

| Step | Why |
|---|---|
| Create a Gmail/IMAP label or dedicated mailbox named `job-alerts` | Limits what the bot can read |
| Forward LinkedIn/Wellfound/job-board alerts there | Avoids logged-in scraping |
| Add IMAP read-only credentials/app password to `.env` | Lets the bot parse alerts |

The bot does not send email and does not log into LinkedIn.

## 5. Final Human Approval Boundary

The bot may:

- collect jobs
- rank jobs
- send Telegram digests
- generate cover-note drafts
- mark user feedback

The bot may not:

- submit applications
- send recruiter messages
- automate logged-in LinkedIn/Wellfound sessions
- mount browser profiles or cookies
- access files outside this project volume

