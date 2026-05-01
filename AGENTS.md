# Agents Guide

Orientation for AI coding agents (Claude Code, Codex, Cursor, etc.) and human contributors. Read this before reading the README. The README describes the *intended* user experience; this file is honest about what is built vs. what is specified.

## What this project is

A safe, low-cost job-search assistant that runs as **two cooperating Docker containers**:

1. **`jobbot`** — deterministic Python service. Collects jobs from public sources, scores them with a rule interpreter, sends digests to Telegram, and handles user button clicks. Stdlib-only.
2. **`openclaw-gateway`** — agent runtime. Runs *only* when invoked by `jobbot` for source discovery (`Update sources`) or scoring-algorithm tuning (`Tune scoring`). Uses Codex via the user's subscription.

The user interacts entirely through Telegram. There is no web UI. The bot **never** applies to jobs, messages recruiters, or sends email.

## The mental model that matters most

**Everything is on-demand.** There is no cron, no background polling. The user clicks a Telegram button, work happens, results land in chat. This is the single biggest difference from what the previous spec described — and from what the current implementation still does.

Three primary entry points:

- **`Get more jobs`** → on-demand collection across enabled sources → fresh digest of jobs not previously shown.
- **`Update sources`** → `jobbot` writes a request file to a shared workspace volume; OpenClaw + Codex iterate (propose → validate → refine) and write a response; jobbot posts an approval prompt; user approves; sources land in `config/sources.json` with `created_by='agent'`.
- **`Tune scoring`** → same shape as discovery but for `config/scoring.json`. Includes a **shadow test** before activation: re-score the last 100 jobs with the proposed rules, report distribution shift + agreement vs Applied/Irrelevant feedback, only then offer `[Apply][Reject]`.

Two LLM tiers, kept strictly separate:

- **Codex (subscription, flat fee)** — only inside the OpenClaw container, only for source discovery and scoring tuning.
- **OpenAI API (paid, budget-gated)** — only inside `jobbot`, only for cover-note generation. Daily/monthly $ caps + per-day count cap + Telegram override prompt on overage.

Per-job scoring is **fully deterministic** — zero LLM calls per job. The agent updates the *rules*; the rules score the jobs.

## Source of truth

| Document | Use it for |
|---|---|
| [`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md) | The intended product. This is the spec. |
| [`tasks.md`](tasks.md) | The honest gap list. What's built vs. what's specified, prioritized. Every work item lives here, in the table only. |
| [`README.md`](README.md) | User-facing setup. **Note: parts are still stale relative to the revised spec** (see tasks.md #46). Trust the spec + tasks.md when they conflict. |
| `git log` | Recent code changes. Authoritative for who-did-what. |

## Repository layout

```
.
├── jobbot/                       # Python service (stdlib-only)
│   ├── __main__.py               # CLI entry: init, collect, digest, serve, ...
│   ├── app.py                    # JobBot orchestrator (god object — slated for split)
│   ├── config.py                 # Settings + AppConfig dataclass
│   ├── models.py                 # SourceConfig, UserProfile, Job, ScoreResult, TelegramAction
│   ├── database.py               # SQLite layer + schema; stable_job_id
│   ├── sources.py                # Per-source collectors (RSS, Remotive, RemoteOK, Arbeitnow, JSON, IMAP)
│   ├── scoring.py                # Hardcoded scoring (TO BE REPLACED with rule interpreter — tasks.md #6)
│   ├── llm.py                    # OpenAI client (cover notes only, after revision)
│   ├── budget.py                 # Daily/monthly $ gate
│   └── telegram.py               # Telegram client + callback parser
├── openclaw/
│   └── JOB_SEARCH_AGENT_PROMPT.md  # Prompt for the OpenClaw agent (no app code yet — tasks.md #12)
├── config/
│   ├── sources.json              # Public sources (manual + agent-discovered after #13)
│   ├── profile.json              # Optional structured profile overrides
│   ├── jobbot.json               # Budgets, model, digest size
│   └── scoring.json              # NOT YET — will hold the rule DSL (tasks.md #6)
├── input/
│   ├── profile.md                # Free-text job profile description (primary input per spec §6.1)
│   └── cv.md                     # Optional CV (text). Cover-note context only (tasks.md #27)
├── data/
│   └── jobs.sqlite               # Local state (gitignored)
├── docs/
│   └── OPENCLAW_DOCKER_APPROVAL_STEPS.md
├── tests/                        # 7 unit tests (none cover app.py — tasks.md #33)
├── docker-compose.yml            # Two services; SHARED VOLUME MISSING — tasks.md #1
├── Dockerfile                    # No HEALTHCHECK — tasks.md #31
├── pyproject.toml                # pytest config only; no linter — tasks.md #47
├── OPENCLAW_JOB_SEARCH_SPEC.md   # The spec
├── tasks.md                      # The work list
├── README.md                     # User docs
├── AGENTS.md                     # This file
└── CLAUDE.md                     # Pointer to AGENTS.md
```

## Conventions

- **Python ≥ 3.9. Stdlib only.** No `requests`, no `feedparser`, no `python-telegram-bot`, no `openai`. Tradeoff: more boilerplate, but tiny image, zero supply-chain surface, fast cold start. Don't add a dependency without an explicit ask.
- **No comments unless the WHY is non-obvious.** Don't restate what the code does. Don't reference issues/tasks/PRs in code comments. Don't write multi-line docstrings.
- **Word-boundary matching only** (`\bterm\b`) for any rule applied to job text. Substring matching has caused real bugs (`intern` matching `international`, `us only` matching `trust only`).
- **Never put secrets in URLs or logs.** Telegram bot token will leak via urllib's URL repr if uncaught — see tasks.md #35.
- **Prefer editing existing files over creating new ones.** Don't add docs/READMEs/notes unless asked.
- **Telegram messages are plain text** (no Markdown/HTML parser). Don't introduce parsing without changing every call site.
- **All cross-container IO is file-based** in `/jobbot/workspace/{discovery,tuning}/`. No HTTP between containers, no shared SQLite.

## Build / test / run

```bash
# Tests (currently 7, all passing)
python3 -m unittest discover -s tests
# or
python3 -m pytest tests/ -q

# Local smoke (no Docker)
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest

# Docker
docker compose build jobbot
docker compose up -d jobbot
docker compose --profile openclaw up -d openclaw-gateway   # optional today; mandatory once tasks.md #12 lands

# Compose validity check
docker compose --profile openclaw config --quiet
```

There is **no lint step** today. There is **no CI** today.

If Python bytecode compilation tries to write outside the sandbox, set `PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache`.

## What the spec says vs. what is implemented

This is the high-trust summary. For full priority/acceptance/impact, read [`tasks.md`](tasks.md).

### Architecture

| Spec | Implementation |
|---|---|
| Shared workspace volume between jobbot and openclaw containers | **Missing.** The two services share zero volumes. |
| OpenClaw container runs an agent runtime (file watch + Codex client + validator tools) | **Not built.** The container runs the stock OpenClaw image with no app code. |
| File-based contract: `discovery/{request,status,response}-<ts>.json`, `tuning/{request,status,response}-<ts>.json` | **Not built.** |
| jobbot's `serve` is a Telegram-poll-only loop | jobbot's `serve` is a 4-hour collection cron. **Wrong shape.** |

### Telegram

| Spec | Implementation |
|---|---|
| Per-job buttons: Irrelevant / Snooze / Cover note / Applied | ✅ |
| Bot-level buttons: `Get more jobs`, `Update sources`, `Tune scoring`, `Usage` | **None of them.** Callback parser only handles `job:*`. |
| Approval callbacks: `disc:approve:<sid>:<idx>`, `disc:reject:<sid>`, `tune:apply:<sid>`, `tune:reject:<sid>` | **None.** |
| Cover-note budget override via `[Override once][Cancel]` | **None.** Silently falls back to template. |
| Follow-up prompts (Why irrelevant? / Where applied? / Tone?) | **None.** Action keyword is the only signal captured. |

### Scoring

| Spec | Implementation |
|---|---|
| Rules in `config/scoring.json` interpreted by a fixed Python interpreter | **No file. Hardcoded scoring.** |
| Six rule kinds: `match_any_word`, `match_all_word`, `hard_reject_word`, `field_equals`, `numeric_at_least`, `feedback_similarity` | **None.** |
| Word-boundary matching | **No.** Uses substring; produces false hard-rejects. |
| Algorithm-update flow: write request → OpenClaw+Codex propose → schema-validate → shadow-test → user approve → archive previous version | **None.** |
| `scoring_versions` audit table | **No.** |

### Sources

| Spec | Implementation |
|---|---|
| `Get more jobs` triggers on-demand collection with rate limit | **Cron-driven.** No button. |
| Cross-source dedupe (same canonical URL/title/company across sources) | **Per-source only.** |
| `digest_log` table → no re-spam guarantee | **Missing table.** Same jobs re-shown every cycle. |
| `Update sources` flow: jobbot ↔ OpenClaw ↔ Codex with per-candidate validation | **Single direct OpenAI API call.** Returns a markdown table; no validation; no approval flow; no writes to `sources.json`. |
| `discovery_runs` audit table | **No.** |
| Source lifecycle: `active`/`test`/`disabled` with auto-promotion | **Boolean `enabled` only.** No probation. |
| Per-IMAP-source filter via `query` field (e.g. djinni vs LinkedIn) | **No.** Single global IMAP folder. |

### Profile / CV

| Spec | Implementation |
|---|---|
| Primary: free-text `input/profile.md`; parsed for target_titles + positive_keywords + exclusions | Reads file as opaque text blob; user must hand-author `config/profile.json`. |
| Optional: `input/cv.md` — used only for cover-note context | **No `cv.md` distinction.** `profile.md` doubles as CV. |

### LLM cost

| Spec | Implementation |
|---|---|
| Codex (subscription) for discovery + tuning; OpenAI API only for cover notes | **Only OpenAI API client exists.** No Codex client. |
| Default model is real | Default is `gpt-5.4-nano` — does not exist; all calls 400; fallback template silently. |
| Actual token usage from response, not estimates | Estimates tokens from `len(text)/4`. |
| Cover-note budget override prompt | Silent template fallback. |
| `Usage` button surfaces daily/monthly spend + counts | `python -m jobbot usage` CLI only. |

### Schema

| Spec | Implementation |
|---|---|
| Tables: `candidate_profile`, `sources`, `source_runs`, `jobs`, `job_scores`, `job_feedback`, `drafts`, `usage_log`, `discovery_runs`, `scoring_versions`, `digest_log`, `rate_limits` | Has the first 7 + `usage_log` + an unused `experiments` table. **The four new audit/state tables are missing.** |

### Safety

| Spec | Implementation |
|---|---|
| No browser cookies / no auto-apply / no recruiter messaging | ✅ |
| Narrow Docker mounts | ✅ for jobbot. **Shared workspace volume missing.** |
| `cap_drop: ALL`, `no-new-privileges` | ✅ |
| `mem_limit`/`cpus`/`read_only` rootfs | ❌ |
| `HEALTHCHECK` | ❌ |
| Scheme allowlist + private-IP rejection in HTTP fetcher (SSRF defense) | ❌ |

## Non-negotiables (do not weaken)

- No logged-in LinkedIn/Wellfound browser automation. Email alerts only.
- No mounting browser cookies, real browser profiles, the host home directory, SSH keys, or `/var/run/docker.sock`.
- No auto-apply. The user applies manually and clicks `Applied`.
- No outbound recruiter messaging or email-send capability.
- No bypassing the OpenAI budget gate. Cover notes only run inside the budget envelope or with explicit user override per request.
- No silent edits to `config/sources.json` or `config/scoring.json` — every agent-proposed change goes through a Telegram approval click.
- No committing real CV/profile data, API keys, Telegram tokens, IMAP credentials, SQLite databases, or generated drafts.

## How to extend

### Add a manual source

1. Edit [`config/sources.json`](config/sources.json). Schema today:
   ```json
   {
     "id": "unique-id",
     "name": "Display Name",
     "type": "rss" | "remotive" | "remoteok" | "arbeitnow" | "json_api" | "imap",
     "url": "https://...",
     "enabled": true,
     "risk_level": "low",
     "poll_frequency_minutes": 240,
     "headers": {},
     "query": null
   }
   ```
2. Restart the bot. The source registers on next `init` / `collect`.

### Add an email source (e.g. djinni.co)

**Today:** only one IMAP source supported (single folder). Set up a Gmail label `job-alerts`, route djinni alerts there with a Gmail filter (`from:no-reply@djinni.co`), enable the `email-job-alerts` row in `sources.json`. The bot reads everything in the folder.

**After tasks.md #17 lands:** add a separate IMAP source row per sender, each with its own `query` field holding an IMAP SEARCH expression (`FROM "no-reply@djinni.co"`). djinni and LinkedIn become independent sources with independent stats.

### Add a new collector type

1. New function in [`jobbot/sources.py`](jobbot/sources.py) shaped like `collect_remotive`, returning `List[Job]`.
2. Wire into [`collect_from_source`](jobbot/sources.py:82) dispatch.
3. Add a unit test in `tests/test_sources.py` with a mocked `fetch_text` and a fixture payload.

### Touch the database schema

Today: add `create table if not exists ...` to [`init_schema`](jobbot/database.py:21). For new columns, add an `alter table` guarded by a try/except (no migration framework). After tasks.md #42 lands: register a new version function.

### Touch scoring

Today: edit [`jobbot/scoring.py`](jobbot/scoring.py) directly. After tasks.md #6 lands: edit `config/scoring.json` and the interpreter handles it; the agent flow can also propose changes via `Tune scoring`.

## Things to refuse / ask before doing

- **Don't add a Python dependency** without checking with the user. The stdlib-only constraint is intentional.
- **Don't add a cron / scheduler.** The on-demand model is a deliberate spec choice.
- **Don't add an HTTP API between containers.** File-based contract on the shared volume only.
- **Don't add per-job LLM scoring.** Per-job is deterministic; LLM updates the rules instead.
- **Don't auto-apply, auto-message, or auto-send email.** Hard product constraint.
- **Don't mount browser profiles, host home, or `/var/run/docker.sock`.** Hard safety constraint.
- **Don't `git add data/`** — contains personal job-search history.

## Open questions worth asking the user before acting

- The CV ingestion task (tasks.md #49) needs PDF/DOCX libs — adding `pdftotext` (binary) or `pypdf` (Python). Stdlib-only constraint conflicts. Ask first.
- Codex client integration (tasks.md #12) requires picking an SDK / subscription mode. Ask the user which Codex they have (ChatGPT subscription, GitHub Copilot, etc.) before scaffolding.
- The shared-volume implementation needs a polling vs file-watch decision. Polling is dependency-free; file-watch needs `inotify` (Linux only) or a small library. Ask before building.

## Validation before declaring done

```bash
python3 -m unittest discover -s tests        # tests pass
docker compose --profile openclaw config --quiet  # compose is valid
git diff --check                              # no whitespace/conflict markers
git status -sb                                # no accidental tracked files (data/, .env)
```
