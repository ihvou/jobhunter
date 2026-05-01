# Jobhunter OpenClaw Jobbot

Safe, low-cost job-search assistant for a human-in-the-loop workflow. It runs in Docker, searches only public/API/RSS/email-alert sources, ranks jobs deterministically, and talks to you through Telegram.

The bot never applies to jobs, sends recruiter messages, logs into LinkedIn, mounts browser cookies, or sends email.

Read the product spec in [`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md). Agent/contributor instructions live in [`AGENTS.md`](AGENTS.md).

## What Runs

| Component | Purpose |
|---|---|
| `jobbot` | Python stdlib-only collector, scorer, budget gate, Telegram bot, and approval handler |
| `openclaw-gateway` | Isolated OpenClaw container for source-discovery/scoring-tuning work through the shared workspace |
| SQLite | Local jobs, scores, feedback, digests, drafts, usage, and audit records |
| Telegram | Daily controls and per-job feedback loop |

The normal app has no web UI. Telegram is the control surface.

## Mental Model

Everything is on-demand.

| Telegram Button | What Happens |
|---|---|
| `Get more jobs` | Collects enabled sources once, dedupes, scores, and sends only jobs not already shown |
| `Update sources` | Writes a discovery request into `/jobbot/workspace/discovery`; OpenClaw/Codex writes a response; you approve sources in Telegram |
| `Tune scoring` | Writes a tuning request into `/jobbot/workspace/tuning`; proposed rules are shadow-tested before you can apply them |
| `Usage` | Shows OpenAI API spend and local usage counters |

There is no cron-driven collection. `serve` only polls Telegram and the shared workspace.

## First-Time Setup

### 1. Prepare Your Profile

Your profile is the primary input for search and scoring. It is a plain-language description, not a CV dump.

```bash
cp input/profile.example.md input/profile.local.md
```

Edit `input/profile.local.md` with target roles, role goals, strengths, location constraints, exclusions, and salary floor. The parser extracts useful titles and keywords automatically.

Optional CV context for cover notes:

```bash
cp input/cv.example.md input/cv.local.md
```

The CV is only used for cover-note generation, and only a bounded excerpt is sent to the OpenAI API.

Optional structured overrides:

```bash
cp config/profile.example.json config/profile.local.json
```

Leave `config/profile.local.json` out if the free-text profile is enough.

### 2. Configure Environment

```bash
cp .env.example .env
```

Fill the values you approve:

| Variable | Required | Purpose |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token from BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | yes | Restricts bot access to your chat |
| `OPENAI_API_KEY` | optional | Cover notes only, protected by local budget caps |
| `OPENAI_MODEL` | optional | Defaults to `gpt-4o-mini` |
| `EMAIL_IMAP_*` | optional | Read-only job-alert mailbox/label |

Do not commit `.env`, `input/profile.local.md`, `input/cv.local.md`, `config/profile.local.json`, or `data/`.

### 3. Configure Sources

`config/sources.json` contains seed sources. You can edit it manually, or ask `Update sources` to propose new ones for approval.

Each source has a lifecycle:

| Status | Meaning |
|---|---|
| `active` | Included in `Get more jobs` |
| `test` | Newly agent-discovered; included and flagged as a new source until useful |
| `disabled` | Preserved but skipped |

Agent-discovered sources are appended with `created_by: "agent"` only after you click approval in Telegram.

### 4. Initialize

Local smoke test:

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
```

Docker:

```bash
docker compose build jobbot
docker compose run --rm jobbot python -m jobbot init
docker compose up -d jobbot
```

The bot will send a ready message with the digest-level buttons. Click `Get more jobs` to run the first real collection.

## Daily Usage

Use Telegram for the daily loop.

| Step | Action |
|---:|---|
| 1 | Click `Get more jobs` when you want a fresh search |
| 2 | Mark poor matches as `Irrelevant` |
| 3 | Click `Remind me tomorrow` for maybe-later jobs |
| 4 | Click `Give me cover note` only for jobs worth attention |
| 5 | Apply manually outside the bot |
| 6 | Click `Applied` after submitting |
| 7 | Use `Usage` to check API spend |

Per-job scoring is deterministic and free. The LLM never scores every job.

## OpenClaw And Codex Flows

`jobbot` and `openclaw-gateway` share `./openclaw/workspace`:

| Host Path | jobbot Path | OpenClaw Path |
|---|---|---|
| `./openclaw/workspace/discovery` | `/jobbot/workspace/discovery` | `/openclaw/workspace/discovery` |
| `./openclaw/workspace/tuning` | `/jobbot/workspace/tuning` | `/openclaw/workspace/tuning` |

`Update sources` writes:

```text
/jobbot/workspace/discovery/request-<session>.json
/jobbot/workspace/discovery/status-<session>.json
```

OpenClaw/Codex is expected to write:

```text
/openclaw/workspace/discovery/response-<session>.json
/openclaw/workspace/discovery/status-<session>.json
```

When status is `done`, `jobbot` posts an approval prompt. Approved sources land in `config/sources.json` as `status: "test"`.

`Tune scoring` follows the same pattern in `tuning/`. `jobbot` shadow-tests proposed scoring rules against recent jobs and feedback before offering `Apply`, `Reject`, or `Show diff`.

Important boundary: OpenAI API spend in this repo is only for cover notes. Source discovery and scoring tuning are designed for Codex via the user's subscription inside the OpenClaw side of the workflow.

## Email Alerts

Use email alerts for LinkedIn, Wellfound, Djinni, company alerts, or Google Alerts. The bot reads only the configured IMAP folder/label and never sends email.

`.env`:

```text
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USERNAME=you@example.com
EMAIL_IMAP_PASSWORD=<app password>
EMAIL_IMAP_FOLDER=job-alerts
```

Example source rows can share the same folder while filtering by sender:

```json
{
  "id": "linkedin-alerts",
  "name": "LinkedIn Email Alerts",
  "type": "imap",
  "url": "imap://job-alerts",
  "status": "active",
  "created_by": "user",
  "query": "FROM \"jobs-noreply@linkedin.com\""
}
```

The IMAP collector uses per-source UID high-water marks, so old messages are not reprocessed forever.

## Cost Controls

| Cost Area | Control |
|---|---|
| Per-job scoring | No LLM calls |
| Cover notes | OpenAI API, `gpt-4o-mini` default, daily/monthly budget gate |
| Cover-note overage | Telegram asks for explicit one-time override |
| Source discovery | Codex subscription path through OpenClaw, not OpenAI API |
| Scoring tuning | Codex subscription path through OpenClaw, not OpenAI API |
| Abuse prevention | Collection 1/10 minutes, discovery 3/day, tuning 3/day, cover notes 10/day |

Check local spend:

```bash
docker compose exec jobbot python -m jobbot usage
```

## Commands

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
python3 -m jobbot run-once
python3 -m jobbot telegram-poll
python3 -m jobbot discover-sources
python3 -m jobbot tune-scoring
python3 -m jobbot usage
python3 -m jobbot serve
```

| Command | Use |
|---|---|
| `init` | Create/update SQLite schema, source registry, and workspace directories |
| `collect` | Developer/manual fetch from enabled sources |
| `digest` | Send or print current top matches |
| `run-once` | Init, collect, score, and digest once |
| `telegram-poll` | Poll Telegram callbacks once |
| `discover-sources` | Write a discovery request into the shared workspace |
| `tune-scoring` | Write a scoring-tuning request into the shared workspace |
| `usage` | Print local OpenAI usage summary |
| `serve` | Telegram/workspace polling loop; no scheduled collection |

## Docker

```bash
docker compose up -d jobbot
docker compose --profile openclaw up -d openclaw-gateway
docker compose logs -f jobbot
docker compose --profile openclaw config --quiet
```

The compose file uses `/jobbot/...` paths, read-only root filesystem for `jobbot`, CPU/memory limits, dropped capabilities, a tmpfs `/tmp`, and a heartbeat-based healthcheck.

## Data And Backups

| Path | Contents |
|---|---|
| `data/jobs.sqlite` | Jobs, scores, feedback, drafts, digests, usage logs |
| `config/` | Sources, scoring rules, budgets, local structured profile |
| `input/profile.local.md` | Private search profile |
| `input/cv.local.md` | Private CV context |
| `openclaw/workspace/` | Transient source/tuning request/response JSON |

Back up SQLite safely:

```bash
mkdir -p data/backup
sqlite3 data/jobs.sqlite ".backup 'data/backup/jobs-$(date +%Y%m%d-%H%M%S).sqlite'"
```

## Troubleshooting

| Symptom | Check |
|---|---|
| No Telegram messages | Verify bot token, allowed chat ID, and `docker compose logs jobbot` |
| `Get more jobs` says wait | Collection rate limit is working; try again after the shown wait |
| Digest is empty | Inspect logs, loosen scoring rules, or add/approve more sources |
| Same job repeats | Check `digest_log`; snoozed due jobs are allowed to reappear once |
| Cover note denied | Use `Usage` and budget env vars; approve the one-time override only if intended |
| OpenClaw proposal never appears | Inspect `openclaw/workspace/*/status-*.json` and OpenClaw container logs |
| Email alerts missing | Confirm IMAP folder, app password, source `status`, and source `query` |

## Safety Notes

- Do not mount browser profiles, cookies, home directories, SSH keys, or `/var/run/docker.sock`.
- Do not enable email sending.
- Do not give the bot recruiter messaging credentials.
- Keep OpenClaw bound to localhost unless you deliberately add auth and network controls.
- Keep OpenAI keys scoped to a dedicated low-budget project.
- Apply manually; then click `Applied`.
