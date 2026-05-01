# Jobhunter OpenClaw Jobbot

This repository contains a safe, low-cost MVP for an OpenClaw-assisted job-search workflow.
It is designed to run as an autonomous scout and drafter, while keeping job applications,
recruiter messages, and account-sensitive actions under human control.

The implementation is intentionally conservative:

- no LinkedIn logged-in browser automation
- no browser profile or cookie mounts
- no auto-apply
- no recruiter messaging
- Telegram-only human feedback loop
- local SQLite audit log
- local daily/monthly LLM budget gate

Read the full product and implementation spec in
[`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md).

## What Runs

| Component | Purpose |
|---|---|
| `jobbot` | Deterministic job collector/ranker/Telegram bot |
| `openclaw-gateway` | Optional full OpenClaw Gateway container, isolated to local volumes |
| SQLite | Jobs, scores, feedback, drafts, usage logs |
| Telegram Bot | Digest and inline feedback buttons |

## How It Works

| Step | What Happens |
|---:|---|
| 1 | Collect jobs from public RSS/API sources and optional scoped email alerts |
| 2 | Normalize and deduplicate jobs into SQLite |
| 3 | Score each job against your profile and preferences |
| 4 | Send the best matches to Telegram |
| 5 | Learn from your inline feedback |
| 6 | Generate cover notes only when you request them |
| 7 | Track cost, source quality, rejected jobs, drafts, and applied jobs |

The bot can run without OpenAI credentials. In that mode, it uses rules and fallback templates.
With an OpenAI API key, it can generate better cover notes and source-discovery recommendations,
subject to the local daily/monthly budget limits.

## First-Time Setup

### 1. Prepare Your Profile

Copy [`input/profile.example.md`](input/profile.example.md) to `input/profile.local.md`, then replace
the placeholder text with a concise text export of your CV.

```bash
cp input/profile.example.md input/profile.local.md
```

Recommended sections:

- target roles
- core skills
- recent work history
- selected achievements
- location and timezone constraints
- salary floor
- dealbreakers

Copy [`config/profile.example.json`](config/profile.example.json) to `config/profile.local.json`, then
tune the local file:

```bash
cp config/profile.example.json config/profile.local.json
```

| Field | Purpose |
|---|---|
| `target_titles` | Titles that should score highly |
| `positive_keywords` | Skills/domains that should boost scores |
| `negative_keywords` | Terms that should lower or reject jobs |
| `required_locations` | Preferred remote/location/timezone terms |
| `excluded_locations` | Location patterns to reject |
| `excluded_domains` | Industries or domains to reject |
| `salary_floor` | Optional minimum compensation filter |

### 2. Configure Sources

Edit [`config/sources.json`](config/sources.json).

The default enabled sources are safe public RSS/API sources:

| Source | Type | Notes |
|---|---|---|
| Remotive | API | Remote software jobs |
| RemoteOK | API | Remote jobs |
| Arbeitnow | API | Remote/public job feed |
| We Work Remotely | RSS | Remote job feed |

There is also a disabled IMAP source named `email-job-alerts`. Enable it only after you set up a
dedicated job-alert mailbox or folder.

### 3. Create Local Environment

Copy the example env file:

```bash
cp .env.example .env
```

Fill only the values you approve:

| Variable | Required | Purpose |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes for Telegram | Token from BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | yes for Telegram | Restricts bot output to your chat |
| `OPENAI_API_KEY` | optional | Enables LLM cover notes and source discovery |
| `OPENAI_MODEL` | optional | Defaults to `gpt-5.4-nano` |
| `JOBBOT_DAILY_BUDGET_USD` | recommended | Local daily spend cap |
| `JOBBOT_MONTHLY_BUDGET_USD` | recommended | Local monthly spend cap |
| `EMAIL_IMAP_*` | optional | Read-only job-alert mailbox access |

Do not commit `.env`, `input/profile.local.md`, or `config/profile.local.json`. They are ignored by git.

### 4. Run A Local Smoke Test

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
```

Without Telegram credentials, the digest prints to stdout.

### 5. Start With Docker

```bash
docker compose build jobbot
docker compose run --rm jobbot python -m jobbot init
docker compose up -d jobbot
```

Check logs:

```bash
docker compose logs -f jobbot
```

Stop the bot:

```bash
docker compose stop jobbot
```

Restart after config changes:

```bash
docker compose restart jobbot
```

## Optional OpenClaw Gateway

The deterministic `jobbot` service handles the safety-critical workflow. The optional OpenClaw
Gateway container is for higher-level strategy, supervision, and future extension.

Start it only after you approve OpenClaw onboarding/configuration:

```bash
docker compose --profile openclaw up -d openclaw-gateway
```

Open:

```text
http://127.0.0.1:18789/
```

Use [`openclaw/JOB_SEARCH_AGENT_PROMPT.md`](openclaw/JOB_SEARCH_AGENT_PROMPT.md) as the dedicated
job-search strategy agent prompt.

See [`docs/OPENCLAW_DOCKER_APPROVAL_STEPS.md`](docs/OPENCLAW_DOCKER_APPROVAL_STEPS.md) for the
approval-gated setup steps.

## Daily Usage

The normal daily flow is Telegram-first.

| Time | Action |
|---|---|
| Morning | Read the digest and mark each job |
| During day | Click `Give me cover note` for jobs worth applying to |
| After applying manually | Click `Applied` |
| End of day | Check usage and review missed/snoozed jobs if needed |

Useful commands:

```bash
docker compose logs --tail=100 jobbot
docker compose exec jobbot python -m jobbot usage
docker compose exec jobbot python -m jobbot discover-sources
docker compose exec jobbot python -m jobbot digest
```

If running without Docker:

```bash
python3 -m jobbot usage
python3 -m jobbot discover-sources
python3 -m jobbot digest
```

## Telegram Workflow

Every job card includes these inline actions:

| Button | When To Use It | Effect |
|---|---|---|
| `Irrelevant` | Bad role, bad location, wrong seniority, wrong domain | Marks job rejected and weakens similar/source signals |
| `Remind me tomorrow` | Interesting, but you are not ready to decide | Snoozes the job for 24 hours |
| `Give me cover note` | You want to apply or inspect fit more deeply | Generates and stores a tailored cover note |
| `Applied` | You applied manually outside the bot | Marks the job applied and strengthens source/actionability signals |

Important: the bot does not submit applications. You apply manually, then click `Applied`.

## Email Alerts

LinkedIn and other logged-in platforms should be used through email alerts, not browser automation.
The bot reads a single scoped IMAP folder and extracts links from messages. It never sends email
and never logs into the source platform.

### One-Time Mailbox Setup

| Step | Purpose |
|---|---|
| Create a dedicated mailbox **or** a Gmail label named `job-alerts` | Keeps the bot's read scope narrow |
| Configure provider filters so all incoming alert emails land in that folder/label | Avoids the bot ever seeing your other mail |
| Generate an IMAP app-password (Gmail: Account → Security → App passwords) | Plain account password is rejected by most providers |
| Add IMAP credentials to `.env` | Read-only access from the container |
| Enable `email-job-alerts` in `config/sources.json` (set `enabled: true`) | Starts parsing alert links on the next collection |

`.env` keys consumed by the IMAP collector:

```text
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USERNAME=you@example.com
EMAIL_IMAP_PASSWORD=<app password>
EMAIL_IMAP_FOLDER=job-alerts
```

### Worked Example: Adding djinni.co Alerts

djinni.co does not expose a public RSS/API for personal job alerts, but it does email them. So the
bot consumes djinni alerts the same way it consumes LinkedIn alerts — via the scoped IMAP folder.

| Step | Action |
|---|---|
| 1 | Sign in to djinni.co and configure your job-alert subscription (role, level, location, keywords) |
| 2 | Set the alert recipient to the same address you configured in `.env` |
| 3 | In Gmail (or your provider): create a filter `from:(no-reply@djinni.co)` → "Apply label `job-alerts`" + "Skip the inbox" |
| 4 | In `config/sources.json`, set `email-job-alerts.enabled` to `true`. No new entry is needed — the IMAP collector picks up every message in the labeled folder regardless of sender |
| 5 | Trigger a collection. New djinni jobs appear in the next digest |

Repeat the filter step for any other alert sender (LinkedIn `jobs-noreply@linkedin.com`,
Wellfound `team@wellfound.com`, company alerts, Google Alerts, etc.). All of them land in the same
`job-alerts` folder; one IMAP source covers all of them.

### How The IMAP Collector Treats Each Email

| Behavior | Detail |
|---|---|
| Scope | Reads only `EMAIL_IMAP_FOLDER`; never the inbox |
| Mode | Read-only (the bot never marks messages SEEN or moves them) |
| Title | Uses the email subject as the job title |
| Company | Inferred from subject + sender; often "Unknown company" for digest-style alerts |
| URL | Extracts up to 10 URLs per message; one `jobs` row per URL |

**Caveat for digest-style alerts** (djinni weekly digest, LinkedIn "10 new jobs in Kyiv", etc.):
the current MVP parser produces multiple rows per email but they share the same subject as title.
Per-job parsing for the major alert formats is planned (see [tasks.md](tasks.md)). For now, the
links are still useful — clicking through opens the real job page on the source platform — but
the digest cards from email-derived jobs look generic until per-format parsing is implemented.

### Variants

| Need | Setup |
|---|---|
| Multiple alert sources, one folder | Default. One filter per sender → same `job-alerts` label |
| Separate folders per source | Add a second source row in `config/sources.json` with a different `EMAIL_IMAP_FOLDER` (requires an env override per source — currently a single global folder is supported; multi-folder is on the roadmap) |
| Self-hosted/non-Gmail IMAP | Replace `imap.gmail.com` with your provider's host; ensure SSL on port 993 |

## Cost Controls

The bot has a local budget gate before making LLM calls.

| Setting | File / Env | Default |
|---|---|---:|
| Daily budget | `JOBBOT_DAILY_BUDGET_USD` | `$0.50` |
| Monthly budget | `JOBBOT_MONTHLY_BUDGET_USD` | `$10.00` |
| Model | `OPENAI_MODEL` | `gpt-5.4-nano` |
| Digest size | `config/jobbot.json` | `10` |
| Collection interval | `config/jobbot.json` | `240` minutes |

Check local spend:

```bash
docker compose exec jobbot python -m jobbot usage
```

Practical cost guidance:

| Mode | Expected Cost |
|---|---:|
| No OpenAI key | `$0` LLM cost |
| Cheap cover notes and source discovery | Usually under `$10/month` |
| Heavy analysis of many jobs | Increase budgets deliberately |

Create a dedicated OpenAI API project/key for this bot so provider-side usage is easy to monitor.

## Config Files

| File | Purpose |
|---|---|
| `config/profile.example.json` | Safe committed template for profile settings |
| `config/profile.local.json` | Your private local profile settings, ignored by git |
| `input/profile.example.md` | Safe committed template for CV/profile text |
| `input/profile.local.md` | Your private local CV/profile text, ignored by git |
| `config/sources.json` | Public RSS/API/email sources |
| `config/jobbot.json` | Budgets, model, digest size, collection interval |
| `.env` | Secrets and runtime paths |

## Source Discovery

Run this weekly or when the search quality feels stale:

```bash
docker compose exec jobbot python -m jobbot discover-sources
```

The bot reviews:

- `input/profile.local.md`
- `config/profile.local.json` if present, otherwise `config/profile.example.json`
- source performance metrics from SQLite
- recent feedback such as `Irrelevant`, `Give me cover note`, and `Applied`

It then proposes new public sources, search patterns, company lists, ATS pages, or communities to
try. Source Discovery is recommendation-only in this MVP: it does not silently edit
`config/sources.json`, enable scraping, or start polling a new platform. That review step is
intentional. Add or enable sources only after you like the recommendation and the access method is
safe.

Typical workflow:

```bash
docker compose exec jobbot python -m jobbot discover-sources
# review recommendations
# manually edit config/sources.json
docker compose restart jobbot
```

This means you still configure seed sources through `config/sources.json`, but you do not have to
know every source up front. The bot can suggest sources that match your profile and your feedback
history; you decide which ones become active.

## Commands

```bash
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
python3 -m jobbot run-once
python3 -m jobbot telegram-poll
python3 -m jobbot discover-sources
python3 -m jobbot usage
python3 -m jobbot serve
```

| Command | Use |
|---|---|
| `init` | Create/update SQLite schema and source registry |
| `collect` | Fetch and score jobs from configured sources |
| `digest` | Send/print current top matches |
| `run-once` | Init, collect, score, and digest once |
| `telegram-poll` | Poll Telegram callbacks once |
| `discover-sources` | Generate source recommendations |
| `usage` | Show local LLM spend summary |
| `serve` | Run the scheduled collection and Telegram loop |

## Data And Backups

| Path | Contents |
|---|---|
| `data/jobs.sqlite` | Jobs, scores, feedback, drafts, usage logs |
| `config/` | Search configuration |
| `input/profile.local.md` | Your private text CV/profile |

Back up the SQLite database if you care about historical learning:

```bash
cp data/jobs.sqlite data/jobs.sqlite.backup
```

Avoid committing `data/`; it may contain personal job-search history and generated drafts.

## Troubleshooting

| Symptom | Check |
|---|---|
| No Telegram messages | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` |
| Digest is empty | Run `collect`, inspect logs, loosen filters in `config/profile.local.json` |
| Cover notes are generic | Add more CV detail to `input/profile.local.md` |
| LLM calls stopped | Check `python3 -m jobbot usage` and budget env vars |
| Email alerts not parsed | Confirm IMAP folder name and enable `email-job-alerts` |
| Too many bad jobs | Use `Irrelevant` consistently and tighten negative keywords |
| Too few jobs | Add sources or run `discover-sources` |

## Safety Notes

Docker isolation lowers blast radius, but the real guardrail is removing harmful capabilities:

- do not mount browser profiles
- do not mount your home directory
- do not enable email sending
- do not give the bot recruiter messaging credentials
- keep OpenClaw bound to localhost
- keep OpenAI API key scoped to a dedicated low-budget project

## Suggested Daily Habit

1. Read the Telegram digest.
2. Mark obvious bad fits as `Irrelevant`.
3. Snooze maybe-interesting jobs with `Remind me tomorrow`.
4. Request cover notes only for jobs worth real attention.
5. Apply manually.
6. Click `Applied` after you submit.
7. Check usage once every few days.

That loop gives the bot enough feedback to improve sources and scoring without giving it unsafe
authority over your accounts or applications.
