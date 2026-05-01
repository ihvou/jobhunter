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

Edit [`input/profile.md`](input/profile.md) and replace the placeholder text with a concise text
export of your CV.

Recommended sections:

- target roles
- core skills
- recent work history
- selected achievements
- location and timezone constraints
- salary floor
- dealbreakers

Then tune [`config/profile.json`](config/profile.json):

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

Do not commit `.env`. It is ignored by git.

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

Recommended setup:

| Step | Purpose |
|---|---|
| Create a dedicated mailbox or folder named `job-alerts` | Keeps email scope narrow |
| Forward LinkedIn/Wellfound/company alerts there | Avoids logged-in scraping |
| Add IMAP credentials to `.env` | Lets the bot read only alert emails |
| Enable `email-job-alerts` in `config/sources.json` | Starts parsing alert links |

The bot does not send email and does not log into LinkedIn.

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
| `config/profile.json` | Target roles, keywords, exclusions, salary floor |
| `input/profile.md` | Text CV/profile used by ranking and cover notes |
| `config/sources.json` | Public RSS/API/email sources |
| `config/jobbot.json` | Budgets, model, digest size, collection interval |
| `.env` | Secrets and runtime paths |

## Source Discovery

Run this weekly or when the search quality feels stale:

```bash
docker compose exec jobbot python -m jobbot discover-sources
```

The bot reviews your profile and source metrics, then proposes new public sources, search patterns,
company lists, ATS pages, or communities to try. New sources should be reviewed before enabling
aggressive polling.

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
| `input/profile.md` | Your text CV/profile |

Back up the SQLite database if you care about historical learning:

```bash
cp data/jobs.sqlite data/jobs.sqlite.backup
```

Avoid committing `data/`; it may contain personal job-search history and generated drafts.

## Troubleshooting

| Symptom | Check |
|---|---|
| No Telegram messages | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` |
| Digest is empty | Run `collect`, inspect logs, loosen filters in `config/profile.json` |
| Cover notes are generic | Add more CV detail to `input/profile.md` |
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
