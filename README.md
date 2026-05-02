# Jobhunter OpenClaw Jobbot

Safe, low-cost job-search assistant for a human-in-the-loop workflow. It runs in Docker, searches only public/API/RSS/email-alert sources, ranks jobs deterministically, and talks to you through Telegram.

The bot never applies to jobs, sends recruiter messages, logs into LinkedIn, mounts browser cookies, or sends email.

Read the product spec in [`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md). Agent/contributor instructions live in [`AGENTS.md`](AGENTS.md).

## What Runs

| Component | Purpose |
|---|---|
| `jobbot` | Python stdlib-only collector, scorer, budget gate, Telegram bot, and approval handler |
| `openclaw-gateway` | Isolated Codex CLI worker for source-discovery/scoring-tuning work through the shared workspace |
| SQLite | Local jobs, scores, feedback, digests, drafts, usage, and audit records |
| Telegram | Persistent daily-control keyboard and per-job feedback loop |

The normal app has no web UI. Telegram is the control surface.

## Mental Model

Everything is on-demand.

| Telegram Keyboard Button | What Happens |
|---|---|
| `Get more jobs` | Collects enabled sources once, dedupes, L1-scores, runs the bounded L2 relevance pass, and sends only jobs not already shown |
| `Update sources` | Routes a canned `/agent` request to OpenClaw/Codex; proposed source edits require Telegram approval |
| `Tune scoring` | Routes a canned `/agent` request; proposed rule edits require Telegram approval and are audit/revert tracked |
| `Usage` | Routes a canned `/agent` data request for usage/quota analysis |

There is no cron-driven collection. `serve` only polls Telegram and the shared workspace.

## First-Time Setup

### 1. Prepare Your Profile

Your profile is the primary input for search and scoring. It is a plain-language description, not a CV dump.

```bash
cp input/profile.example.md input/profile.local.md
```

Edit `input/profile.local.md` with target roles, role goals, strengths, location constraints, exclusions, and salary floor. The parser extracts useful titles and keywords automatically.

Use one file with two sections:

```markdown
# About me

Describe your background, target roles, constraints, and preferences.

# Directives

[2026-05-02] Skip Product Marketing Manager, MLOps, DevOps, pure engineering, and jobs requiring languages other than English, Ukrainian, or Russian.
```

`# About me` is the stable profile. `# Directives` is the living instruction log used by `/agent`, source discovery, scoring tuning, and the L2 relevance pass.

Optional CV context for cover notes:

```bash
cp input/cv.example.md input/cv.local.md
```

The CV is only used for cover-note generation, and only a bounded excerpt is sent to the OpenAI API.

Legacy `config/profile.local.json` is no longer the preferred source of truth. On init, jobbot folds it into `input/profile.local.md` and backs it up.

### 2. Configure Environment

```bash
cp .env.example .env
```

Fill the values you approve:

| Variable | Required | Purpose |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token from BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | yes | Restricts bot access to your chat |
| `OPENAI_API_KEY` | optional | Cover notes and the optional L2 job relevance pass, protected by local budget caps |
| `OPENAI_MODEL` | optional | Defaults to `gpt-4o-mini` |
| `EMAIL_IMAP_*` | optional | Read-only job-alert mailbox/label |
| `JOBBOT_CODEX_HANDOFF_MODE` | optional | `auto` uses the worker; `manual` sends paste-ready prompts to Telegram |
| `OPENCLAW_CODEX_*` | optional | Codex worker model, poll interval, and timeout |
| `OPENCLAW_AGENT_MAX_*` | optional | Per-agent-turn caps for Codex turns, SQL reads, file reads, HTTP fetches, and prompt size |
| `JOBBOT_L2_MAX_JOBS` | optional | Max jobs per `Get more jobs` click sent to the L2 relevance pass; default `30` |

Do not commit `.env`, `input/profile.local.md`, `input/cv.local.md`, local profile backups, or `data/`.

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
./jobhunter start
```

The bot will send a ready message with a persistent Telegram keyboard. Tap `Get more jobs` to run the first real collection. Slash-command fallbacks also work: `/jobs`, `/sources`, `/tune`, and `/usage`.

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
| 7 | Use `Usage`, `/history`, and `/revert <id>` to audit agent work |

L1 scoring is deterministic and free. After L1, jobbot runs a bounded L2 relevance pass on at most `JOBBOT_L2_MAX_JOBS` candidates per click. With `gpt-4o-mini` this is intended to stay around a few tenths of a cent per click. If `OPENAI_API_KEY` is absent, a local fallback rejects only obvious bad role families (Product Marketing, MLOps, DevOps, SRE, unsupported-language requirements) — works without any LLM but is much coarser than the API-backed pass.

Useful Telegram commands:

| Command | Use |
|---|---|
| `/agent <text>` | Ask OpenClaw/Codex to investigate, answer, and propose bounded actions |
| `/feedback <text>` | Add free-form strategy/filter/source feedback through the same agent path |
| `/ask <text>` | Ask for a read-only data answer using allowed tools |
| `/history` | Show recent agent-applied actions |
| `/revert <id>` | Restore the archived file for a reversible agent action |
| `/profile` | Show current `# About me` and directive count |
| `/profile set <text>` | Replace `# About me`, preserving `# Directives` |
| `/profile refine` | Ask the agent to propose a profile rewrite for approval |
| `/applied`, `/snoozed`, `/irrelevant` | Retrieve recent jobs by status |

## Agent Examples

What `/agent` requests actually look like. Each one routes through OpenClaw/Codex, returns an `answer`, and may include one or more `proposed_actions` you approve per-action in chat.

| You type | Bot returns | Approve to apply |
|---|---|---|
| `/feedback skip jobs whose description is primarily in German` | "I'll add a directive to skip German-language jobs." | `directive_edit` writes a timestamped line under `# Directives`. |
| `/feedback prioritize Product Builder/Vibe Coder roles that build with Claude or Codex; deprioritize generic PM` | "Got it — adding a priority directive that L2 will apply per-job." | `directive_edit`. Next `Get more jobs` reflects it via L2's `priority: high` tag. |
| `/feedback please remove the directive about language` | "I'll drop that directive." | `directive_edit` with a removal patch. |
| `/agent please add this and figure out how to fetch it: https://jobs.dou.ua/vacancies/?category=Product%20Manager` | "I fetched the page, found the RSS at /feeds/?category=Product+Manager, and validated it returns 30 entries." | `sources_proposal` (add the RSS source) + `directive_edit` (mark it priority). |
| `/agent you missed https://weworkremotely.com/remote-jobs/webpt-principal-product-manager from 2 days ago. why?` | "WWR's RSS only returns ~50 most recent items. The job had already rotated off by your last collection. Two options: switch this source to `community` type to scrape the full board, or enable a daily safety-net poll." | `sources_proposal` (change type) **or** `human_followup` (file a task for the safety-net poll). |
| `/ask which sources produced jobs I applied to in the last 30 days?` | "RemoteOK: 4, We Work Remotely: 2, Arbeitnow: 1." | No actions; `data_answer` + `evidence_table` shown inline. |
| `/ask jobs I applied to yesterday` | "You applied to 2 jobs yesterday: Senior PM at Linear (8:14), Applied AI Engineer at Anthropic (15:02)." | No actions; just data. |
| `/agent suggest 3 niche aggregators I'm missing` | "Based on your applied set (AI/LLM startups, product builder roles), I'd add Lovable Jobs, AI Tinkerers job board, and Hacker News Who's Hiring (current month)." | `sources_proposal` with 3 entries; HEAD-probed before approval prompt. |
| `/profile refine` | "Tightened wording. Diff: ..." | `profile_edit` replaces `# About me`; `# Directives` untouched. |
| `/agent stop suggesting individual company career pages — focus on aggregators` | "Captured. Future discovery runs will steer toward aggregators." | `directive_edit`. Next `Update sources` reflects it. |
| `/agent backup my config and profile` | "Created backup at data/backup/jobhunter-2026-05-02.tar.gz (3.2 KB)." | `backup_export` already executed (read-only). |
| `/agent show me what rule fires most often` | "Top firing rules: title_target_product_roles (47×), ai_focus (38×), profile_similarity (24×). 12 jobs hard-rejected by exclude_seniority_title." | No actions; just analysis. |

Each write action is gated behind `[Apply 1] [Apply 2] [Apply all] [Reject all]` buttons. Applied actions are logged in `agent_actions` with the file diff archived; `/revert <id>` restores any reversible file change byte-for-byte.

The bounded action kinds (`directive_edit`, `profile_edit`, `sources_proposal`, `scoring_rule_proposal`, `data_answer`, `human_followup`, `rescore_jobs`, `bulk_update_jobs`, `backup_export`) are the only mutations the agent can request. Anything outside that set is silently dropped — Codex cannot ask jobbot to execute arbitrary code or write outside the allowlisted paths.

## OpenClaw And Codex Flows

`jobbot` and `openclaw-gateway` share `./openclaw/workspace`:

| Host Path | jobbot Path | OpenClaw Path |
|---|---|---|
| `./openclaw/workspace/discovery` | `/jobbot/workspace/discovery` | `/openclaw/workspace/discovery` |
| `./openclaw/workspace/tuning` | `/jobbot/workspace/tuning` | `/openclaw/workspace/tuning` |
| `./openclaw/workspace/agent` | `/jobbot/workspace/agent` | `/openclaw/workspace/agent` |

The OpenClaw service is a small worker image with Codex CLI installed. It watches the shared workspace and invokes `codex exec` automatically when request files appear.

One-time Codex subscription login:

```bash
./jobhunter login
```

That login stores Codex auth in `./openclaw/codex-home`, which is gitignored. The repo does not mount your host home directory or browser profile.

`/agent`, `Update sources`, `Tune scoring`, and `Usage` write:

```text
/jobbot/workspace/agent/request-<session>.json
/jobbot/workspace/agent/status-<session>.json
```

The worker writes:

```text
/openclaw/workspace/agent/response-<session>.json
/openclaw/workspace/agent/status-<session>.json
```

When status is `done`, `jobbot` always shows the agent answer. If the response includes write actions, jobbot renders per-action approval buttons plus `Apply all` / `Reject all`.

The agent response contract is:

```json
{
  "user_intent_summary": "...",
  "answer": "...",
  "evidence_table": [],
  "proposed_actions": [
    {"kind": "directive_edit", "summary": "...", "payload": {}}
  ]
}
```

Supported action kinds are `directive_edit`, `profile_edit`, `sources_proposal`, `scoring_rule_proposal`, `data_answer`, `human_followup`, `rescore_jobs`, `bulk_update_jobs`, and `backup_export`. Unknown kinds are dropped and logged; no action kind executes arbitrary code.

Legacy `discover-sources` and `tune-scoring` CLI commands still write the older `discovery/` and `tuning/` contracts for debugging.

Important boundary: source strategy, parser investigation, and scoring/filter strategy use your Codex CLI login inside the OpenClaw worker. Per-job L2 relevance and cover notes use the OpenAI API only when `OPENAI_API_KEY` is set and budget gates allow it.

The worker exposes only read-only tools to Codex:

| Tool | Guardrail |
|---|---|
| `query_sql` | `SELECT` only, capped rows, SQLite mounted read-only |
| `read_file` / `list_dir` | Allowlisted paths only; `.env`, Codex auth, home, and system paths blocked |
| `http_fetch` | HTTP(S) only, private hosts blocked, timeout and excerpt capped |

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
| L1 per-job scoring | No LLM calls |
| L2 job relevance | OpenAI API on top L1 candidates only, default max `30` per click, cached per job |
| Cover notes | OpenAI API, `gpt-4o-mini` default, daily/monthly budget gate |
| Cover-note overage | Telegram asks for explicit one-time override |
| Source/filter strategy | Codex subscription path through OpenClaw, not OpenAI API |
| Agent guardrails | 10s cooldown, 20/day default, capped Codex turns, SQL reads, file reads, HTTP fetches |
| Abuse prevention | Collection 1/10 minutes, agent 20/day, cover notes 10/day, write actions require Telegram approval |

Check local spend:

```bash
./jobhunter status
```

## Operator Launcher

Use `./jobhunter` for day-to-day operation. It wraps the Docker Compose commands and keeps destructive actions behind confirmation prompts.

```bash
./jobhunter help
./jobhunter start
./jobhunter status
./jobhunter logs
./jobhunter logs worker
./jobhunter restart
./jobhunter stop
```

| Command | Use |
|---|---|
| `./jobhunter start` | Build if needed, then start `jobbot` and `openclaw-gateway` |
| `./jobhunter stop` | Stop both containers |
| `./jobhunter restart` | Rebuild and recreate both containers |
| `./jobhunter logs [jobbot\|worker\|both]` | Follow recent logs; defaults to both |
| `./jobhunter status` | One-line health summary with container state, heartbeat, source count, and last digest |
| `./jobhunter login` | Run Codex device login inside the OpenClaw container |
| `./jobhunter shell [jobbot\|worker]` | Open a shell in a running container |
| `./jobhunter reset` | Stop services and clear local SQLite/workspace files after typing `reset` |

The root path `./jobbot` is already the Python package directory, so the launcher is named `./jobhunter`.

`./jobhunter start` and `./jobhunter restart` validate that `.env` contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` before touching Docker.

## Python Commands

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

The launcher above is recommended. Raw Compose commands are still useful for debugging:

```bash
docker compose --profile openclaw up -d jobbot openclaw-gateway
docker compose logs -f jobbot
docker compose --profile openclaw logs -f openclaw-gateway
docker compose --profile openclaw config --quiet
```

The compose file uses `/jobbot/...` paths, read-only root filesystem for `jobbot`, CPU/memory limits, dropped capabilities, a tmpfs `/tmp`, and a heartbeat-based healthcheck.

## Data And Backups

| Path | Contents |
|---|---|
| `data/jobs.sqlite` | Jobs, scores, feedback, drafts, digests, usage logs |
| `config/` | Sources, scoring rules, and budgets |
| `input/profile.local.md` | Private search profile with `# About me` and `# Directives` |
| `input/cv.local.md` | Private CV context |
| `openclaw/workspace/` | Transient discovery/tuning/agent request/response JSON |

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
| Good jobs are hidden | Inspect L2 verdicts in logs/database; add a `/feedback` directive and use `/agent` to tune filters |
| Same job repeats | Check `digest_log`; snoozed due jobs are allowed to reappear once |
| Cover note denied | Use `Usage` and budget env vars; approve the one-time override only if intended |
| Agent proposal never appears | Run `docker compose --profile openclaw logs -f openclaw-gateway` and inspect `openclaw/workspace/agent/status-*.json` |
| Email alerts missing | Confirm IMAP folder, app password, source `status`, and source `query` |

## Safety Notes

- Do not mount browser profiles, cookies, home directories, SSH keys, or `/var/run/docker.sock`.
- Do not enable email sending.
- Do not give the bot recruiter messaging credentials.
- Keep OpenClaw bound to localhost unless you deliberately add auth and network controls.
- Keep OpenAI keys scoped to a dedicated low-budget project.
- Apply manually; then click `Applied`.
- Treat `/agent` approvals like config changes: read the proposed action summary, apply only what you want, and use `/history` plus `/revert <id>` if a reversible file edit goes sideways.
