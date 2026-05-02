# Jobhunter OpenClaw Jobbot

A safe, low-cost Telegram-driven job-search assistant. Two Docker containers, one chat surface. Architecture and detailed contracts live in [`ARCHITECTURE.md`](ARCHITECTURE.md). Contributor instructions in [`AGENTS.md`](AGENTS.md). Open work in [`tasks.md`](tasks.md).

## What It Does

- Collects jobs on demand from public RSS, JSON APIs, ATS boards, and IMAP email alerts (LinkedIn / Wellfound / Djinni / company alerts) — no logged-in scraping.
- Ranks jobs in two layers: free deterministic L1 rules, then a bounded LLM L2 pass that reads your free-form profile and skips obvious mismatches (wrong role family, wrong language, wrong seniority).
- Sends a Telegram digest of new (not previously shown) jobs with per-job buttons: **Irrelevant**, **Remind me tomorrow**, **Give me cover note**, **Applied**.
- Refines itself through chat: type `/feedback`, `/agent`, or `/ask` and Codex (via your subscription) proposes bounded changes you approve per-action — sources, scoring rules, profile directives, ad-hoc data answers.
- Generates tailored cover notes via OpenAI (paid, budget-gated).
- **Never** applies to jobs, messages recruiters, sends email, logs into LinkedIn, or mounts browser cookies.
- Every change the agent proposes is approval-gated, auto-archived, and one-tap reversible via `/revert <id>`.

## Quick Start

You need: a Telegram bot token, your Telegram chat ID, and a Codex CLI subscription (ChatGPT Pro or equivalent). Optionally, an OpenAI API key for cover notes and the L2 relevance pass.

```bash
# 1. Configure secrets
cp .env.example .env
# edit .env and set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_CHAT_ID
# (optional) set OPENAI_API_KEY for cover notes + better L2 relevance

# 2. Authorize Codex (one-time device login)
./jobhunter login

# 3. Start both containers
./jobhunter start
```

The bot will DM you a ready message with a persistent reply keyboard.

### Required environment variables

| Variable | Required | Notes |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_CHAT_ID` | yes | Your numeric chat ID; restricts the bot to your chat only |
| `OPENAI_API_KEY` | optional | Enables cover-note generation and the OpenAI-backed L2 relevance pass. Without it, L2 falls back to a coarser local heuristic and cover notes use a generic template. |

All other env vars (model, budget caps, agent quotas, IMAP credentials) have sensible defaults; see `.env.example` if you want to tune them.

## How To Use It

### From Telegram (everything you need day-to-day)

After `./jobhunter start`:

1. **Set your profile** — type `/profile set <paste your free-text description>`. Example:
    ```
    /profile set Product manager / product builder. Goal: build product prototypes
    or implement features in existing products with Claude Code, Codex, or related
    AI tooling. Strengths: discovery, product analytics, fast prototyping. Avoid:
    internships, junior-only roles.
    ```
2. **Get your first digest** — tap `Get more jobs`. The bot collects, scores, and sends you the top matches.
3. **React to each card** — tap `Irrelevant` / `Remind me tomorrow` / `Give me cover note` / `Applied`. The card disappears from chat after the action.
4. **Teach the system in plain English** — `/feedback skip jobs that mention German required` or `/feedback prioritize Product Builder roles building with Claude or Codex`. The agent proposes a directive change; you approve; the next `Get more jobs` reflects it.
5. **Refine sources and scoring when needed** — tap `Update sources` or `Tune scoring`. The agent proposes changes; you approve per-candidate or per-rule.

### Operator commands

```bash
./jobhunter start         # start both containers (idempotent — re-runs are safe)
./jobhunter stop          # stop both containers
./jobhunter restart       # rebuild and recreate
./jobhunter status        # one-line health summary
./jobhunter logs          # tail both services
./jobhunter logs worker   # tail only the OpenClaw worker
./jobhunter login         # re-authorize Codex (when token expires)
./jobhunter reset         # nuke local SQLite + workspace files (asks for confirmation)
./jobhunter shell jobbot  # open a shell in a running container
./jobhunter help          # see all subcommands
```

`./jobhunter start` and `./jobhunter restart` refuse to run if `.env` is missing required vars and tell you which ones.

### Telegram commands cheatsheet

| Command | Use |
|---|---|
| `/agent <text>` | Ask the agent to investigate and propose bounded actions |
| `/feedback <text>` | Same path, hinted toward editing your `# Directives` |
| `/ask <text>` | Same path, hinted toward read-only data answers |
| `/profile` | Show your current `# About me` content + directive count |
| `/profile set <text>` | Replace `# About me` (preserves directives, two-tap diff confirmation) |
| `/profile refine` | Codex cleans up wording without changing intent; you approve the diff |
| `/history` | Last 10 agent-applied actions |
| `/revert <id>` | Restore the archived file for a reversible agent action |
| `/applied`, `/snoozed`, `/irrelevant` | Retrieve recent jobs by status (since cards leave the chat after each action) |
| `/jobs`, `/sources`, `/tune`, `/usage` | Slash equivalents of the four reply-keyboard buttons |

## Agent Examples

What `/agent` requests actually look like. Each one routes through OpenClaw + Codex, returns an `answer`, and may include one or more `proposed_actions` you approve per-action in chat.

| You type | Bot returns | Approve to apply |
|---|---|---|
| `/feedback skip jobs whose description is primarily in German` | "I'll add a directive to skip German-language jobs." | `directive_edit` writes a timestamped line under `# Directives` |
| `/feedback prioritize Product Builder roles that build with Claude or Codex; deprioritize generic PM` | "Got it — adding a priority directive that L2 will apply per-job." | `directive_edit`. Next `Get more jobs` reflects it via L2's `priority: high` tag |
| `/feedback please remove the directive about language` | "I'll drop that directive." | `directive_edit` with a removal patch |
| `/agent please add this and figure out how to fetch it: https://jobs.dou.ua/vacancies/?category=Product%20Manager` | "I fetched the page, found the RSS at /feeds/?category=Product+Manager, and validated it returns 30 entries." | `sources_proposal` (add the RSS) + `directive_edit` (mark it priority) |
| `/agent you missed https://weworkremotely.com/remote-jobs/webpt-principal-product-manager from 2 days ago. why?` | Root-cause analysis with two repair options | `sources_proposal` (change source type) **or** `human_followup` (file a task) |
| `/ask which sources produced jobs I applied to in the last 30 days?` | "RemoteOK: 4, We Work Remotely: 2, Arbeitnow: 1." | None; `data_answer` shown inline |
| `/ask jobs I applied to yesterday` | List with timestamps | None; just data |
| `/agent suggest 3 niche aggregators I'm missing` | Three candidates with rationale | `sources_proposal` with 3 entries; HEAD-probed before approval prompt |
| `/profile refine` | "Tightened wording. Diff: ..." | `profile_edit` replaces `# About me`; `# Directives` untouched |
| `/agent stop suggesting individual company career pages — focus on aggregators` | "Captured. Future discovery runs will steer toward aggregators." | `directive_edit` |
| `/agent backup my config and profile` | Path to the new tar.gz | `backup_export` already executed |
| `/agent show me what rule fires most often` | Top-firing rules with counts | None; just analysis |

Every write action is gated behind `[Apply 1] [Apply 2] [Apply all] [Reject all]` buttons. Applied actions are logged with the file diff archived; `/revert <id>` restores any reversible file change byte-for-byte.

## Cost Controls

| Item | Default cap |
|---|---|
| L1 per-job scoring | Free (no LLM) |
| L2 relevance pass | OpenAI on top L1 candidates only, ≤ `JOBBOT_L2_MAX_JOBS=30` per click, cached per job (~$0.003/click typical) |
| Cover notes | OpenAI, `gpt-4o-mini`, daily/monthly budget gate, 10/day, one-tap override on overage |
| Agent requests | Codex subscription (no per-call cost), 20/day, 10s cooldown, capped per-request (5 turns / 20 SQL / 10 file reads / 5 fetches / 180s) |
| Bulk write actions | Approval tap PLUS typed `CONFIRM <id>` reply within 60s |
| Collection | 1 / 10 minutes per `Get more jobs` |

Check current spend any time via the `Usage` button, `/usage` command, or `./jobhunter status`.

## Email Alerts (Optional)

Use email alerts for sites that don't expose a public RSS / API but do offer email notifications (LinkedIn, Wellfound, Djinni, company alerts, Google Alerts). The bot reads only the configured IMAP folder and **never sends email**.

```text
# .env
EMAIL_IMAP_HOST=imap.gmail.com
EMAIL_IMAP_USERNAME=you@example.com
EMAIL_IMAP_PASSWORD=<Gmail app password>
EMAIL_IMAP_FOLDER=job-alerts
```

Then add a source row per sender (you can do this from Telegram via `/agent please add a source for djinni.co alerts at FROM no-reply@djinni.co`):

```json
{
  "id": "linkedin-alerts",
  "name": "LinkedIn Email Alerts",
  "type": "imap",
  "url": "imap://job-alerts",
  "status": "active",
  "priority": "high",
  "created_by": "user",
  "query": "FROM \"jobs-noreply@linkedin.com\""
}
```

The IMAP collector tracks per-source UID high-water marks, so old messages are not reprocessed forever.

## Troubleshooting

| Symptom | Check |
|---|---|
| No Telegram messages | Verify bot token + chat ID; `./jobhunter logs jobbot` |
| `Get more jobs` says wait | Collection rate limit kicked in; try after the shown wait |
| Digest is empty | `/agent please tune scoring to be more permissive`, or add sources |
| Bad jobs reach the digest | `/feedback <pattern to skip>`; approve the resulting `directive_edit` |
| Good jobs are hidden | `/ask why was [URL] not in my last digest?`, then `/feedback` to teach |
| Same job repeats | Snoozed-due jobs are allowed to reappear once; otherwise check `digest_log` |
| Cover note denied | Use `Usage` to see budget; approve the one-time override only if intended |
| Agent proposal never appears | `./jobhunter logs worker` and inspect `openclaw/workspace/agent/status-*.json` for `state=failed` |
| `Daily agent quota reached` | Default 20/day; raise `JOBBOT_RATE_LIMIT_AGENT_PER_DAY` in `.env` |
| Codex login expired | `./jobhunter login` re-runs device auth |
| `/revert` says "no reversible archive" | Some action kinds (data_answer, human_followup, bulk_update_jobs, rescore_jobs) don't archive a file; not reversible by `/revert` today |

## Safety Notes

- Do not mount browser profiles, cookies, your home directory, SSH keys, or `/var/run/docker.sock`.
- Do not enable email sending.
- Do not give the bot recruiter messaging credentials.
- Keep OpenAI keys scoped to a dedicated low-budget project.
- Apply manually outside the bot; then tap `Applied`.
- Treat `/agent` approvals like config changes: read each proposed action's summary, apply only what you want, and use `/history` + `/revert <id>` if a reversible file edit goes sideways.

---

# Reference

The rest of this file is reference material. You won't need any of it for normal use — Telegram and `./jobhunter` cover the daily workflow.

## Configuration Files

You usually don't need to touch these. Telegram covers everything once the bot is running. The files exist mostly to bootstrap the very first run and to store the agent's audit trail.

| File | What it is | Day-to-day editing |
|---|---|---|
| `input/profile.local.md` | Your profile (`# About me` + `# Directives`). Required to bootstrap before you can talk to Telegram. | Use `/profile set` and `/feedback` from chat after bootstrap. Direct file edits also work. |
| `input/cv.local.md` | Optional CV text, used only for cover notes. | Edit the file; no Telegram command for CV today. |
| `config/sources.json` | Source registry. Ships with sensible defaults (Remotive, RemoteOK, Arbeitnow, WeWorkRemotely, optional IMAP). | Tap `Update sources` in Telegram or `/agent please add ...`. Direct edits also work. |
| `config/scoring.json` | Active scoring ruleset. Ships with a baseline. | Tap `Tune scoring` in Telegram. Direct edits work but the agent's shadow-test path is safer. |
| `config/jobbot.json` | Budgets and runtime config. | Edit only if the defaults don't fit. |
| `.env` | Secrets and runtime paths. | Edit before first start; rare changes after. |

For the very first run, copy the example files:

```bash
cp .env.example .env
cp input/profile.example.md input/profile.local.md   # or use /profile set after bootstrap
cp input/cv.example.md input/cv.local.md             # optional, only for cover notes
```

The bot auto-creates an empty `input/profile.local.md` if you skip the copy, and you can fill it via `/profile set` immediately. If you have a legacy `config/profile.local.json`, the bot folds it into `# About me` on first init and backs the JSON up.

## Source Lifecycle and Priority

Each source row has a status (`active` / `test` / `disabled`) and a priority (`high` / `medium` / `low`). High-priority sources fetch first per `Get more jobs` click. Agent-discovered sources land as `status: test, created_by: agent` and auto-promote to `active` when you mark a job from them as Applied or request a cover note.

## Python Commands (Development / Debugging)

For day-to-day use, prefer Telegram + `./jobhunter`. The Python CLI is for development and smoke testing.

```bash
python3 -m jobbot init           # init schema, sources, workspace dirs; migrate legacy profile
python3 -m jobbot collect        # fetch from enabled sources (no L2, no Telegram send)
python3 -m jobbot digest         # run L2 on top L1 survivors and send/print top matches
python3 -m jobbot run-once       # init + collect + digest
python3 -m jobbot telegram-poll  # one tick of serve's poll loop
python3 -m jobbot discover-sources  # legacy discovery request file (kept for debugging)
python3 -m jobbot tune-scoring      # legacy tuning request file (kept for debugging)
python3 -m jobbot usage          # local OpenAI usage summary
python3 -m jobbot serve          # Telegram + workspace polling loop; no scheduled collection
```

There's no CLI for `/agent` itself — the agent surface is Telegram-only by design (every action is approval-gated and the chat is the audit log).

## Raw Docker Commands

The launcher above is recommended. Raw Compose is useful for debugging:

```bash
docker compose --profile openclaw up -d jobbot openclaw-gateway
docker compose logs -f jobbot
docker compose --profile openclaw logs -f openclaw-gateway
docker compose --profile openclaw config --quiet
```

## Data and Backups

| Path | Contents | Gitignored? |
|---|---|---|
| `data/jobs.sqlite` | Jobs, L1 scores, L2 verdicts, feedback, digests, drafts, usage, agent_actions audit | yes |
| `data/backup/` | Archives produced by `/agent backup ...` and your manual SQLite snapshots | yes |
| `config/sources.json`, `config/scoring.json`, `config/jobbot.json` | Source registry, scoring rules, runtime config | committed |
| `config/scoring.v<n>.json` | Auto-archived previous scoring versions (used by `/revert`) | committed |
| `input/profile.local.md`, `input/cv.local.md` | Your private profile + optional CV | yes |
| `input/profile.<ts>.md.bak` | Auto-archived previous profile versions | yes |
| `openclaw/workspace/` | Transient `discovery/`, `tuning/`, `agent/` JSON | yes |
| `openclaw/codex-home/` | Codex CLI auth token after `./jobhunter login` | yes |

Manual SQLite backup:

```bash
mkdir -p data/backup
sqlite3 data/jobs.sqlite ".backup 'data/backup/jobs-$(date +%Y%m%d-%H%M%S).sqlite'"
```

Or via the agent: `/agent backup my config and profile`.

## Mental Model and Architecture

For how the two containers talk to each other, the bounded action set, the L1/L2 split, the worker tool surface, and the audit-and-revert chain, see [`ARCHITECTURE.md`](ARCHITECTURE.md). The README intentionally does not duplicate that material.
