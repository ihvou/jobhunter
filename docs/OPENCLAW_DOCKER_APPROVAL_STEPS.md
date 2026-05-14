# OpenClaw Docker Approval Steps

These steps involve accounts, tokens, or external services and should stay under explicit human control.

## 1. Telegram Bot

| Step | Why Approval Is Needed |
|---|---|
| Create bot with BotFather | Creates a real Telegram bot identity |
| Add `TELEGRAM_BOT_TOKEN` to `.env` | Lets OpenClaw receive and send Telegram bot messages |
| Add `TELEGRAM_ALLOWED_CHAT_ID` to `.env` | Restricts bot access to your chat |

## 2. OpenClaw Gateway In Docker

OpenClaw owns Telegram routing, Codex sessions, inline buttons, and calls into Jobhunter through bounded tools.

The repo also ships `plugins/jobhunter-tools/`, a minimal OpenClaw dynamic tool plugin. It calls the same `jobhunter-service` API as the MCP bridge and is loaded so Jobhunter actions appear as OpenClaw trajectory `tool.call` events during acceptance checks. The native Codex MCP registration remains in place for Codex-side tool access and `mcp_tool_call_*` logs.

| Requirement | Current Setting |
|---|---|
| Gateway container | `openclaw-gateway` |
| Service container | `jobhunter-service` |
| Control UI | `127.0.0.1:18789` only |
| Jobhunter tool bridge | stdio MCP: `python3 -m jobhunter.openclaw_mcp` |
| Jobhunter service URL | `http://jobhunter-service:8765` on the Compose network |
| Persistent OpenClaw state | Docker volume `openclaw_home` |
| Codex auth | `~/.codex` mounted read-only |
| Docker socket | not mounted |

Run:

```bash
./bin/openclaw start
./bin/openclaw onboard
```

`./bin/openclaw onboard` must keep three registration steps:

1. Patch OpenClaw config with `mcp.servers.jobhunter`.
2. Write Codex per-agent `[mcp_servers.jobhunter]` with `default_tools_approval_mode = "approve"`.
3. Run `codex mcp add jobhunter -- python3 -m jobhunter.openclaw_mcp`.

## 3. OpenAI API Key

| Step | Why Approval Is Needed |
|---|---|
| Create dedicated OpenAI project/key | Enables paid cover-note calls and higher-quality L2 relevance |
| Set project budget/alerts | Provider-side visibility |
| Add `OPENAI_API_KEY` to `.env` | Lets `jobhunter-service` call OpenAI within local budget caps |

The service also enforces:

```text
JOBHUNTER_DAILY_BUDGET_USD=0.50
JOBHUNTER_MONTHLY_BUDGET_USD=10.00
```

## 4. Email Alerts

For LinkedIn or other logged-in job boards, use email alerts only.

| Step | Why |
|---|---|
| Create an IMAP folder/label named `job-alerts` | Limits what the bot can read |
| Forward job alerts there | Avoids logged-in scraping |
| Add IMAP app password to `.env` | Lets the collector parse alerts |

The bot reads only the configured IMAP folder. It does not send email.

## 5. Final Human Approval Boundary

The bot may:

- collect jobs
- rank jobs
- send Telegram digests through OpenClaw
- generate cover-note drafts
- mark user feedback
- propose source/scoring/profile/parser changes

The bot may not:

- submit applications
- send recruiter messages
- automate logged-in LinkedIn/Wellfound sessions
- mount browser profiles or cookies
- mount `/var/run/docker.sock`
- apply config changes without explicit user approval
