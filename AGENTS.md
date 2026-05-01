# Agents Guide

Orientation for AI coding agents (Claude Code, Codex, Cursor, etc.) and human contributors. Read this before the README. The README describes user setup; this file is the implementation contract.

## What This Project Is

A safe, low-cost job-search assistant that runs as two cooperating Docker containers:

1. **`jobbot`** — deterministic Python service. Collects public/API/RSS/email-alert jobs, scores them with `config/scoring.json`, sends Telegram digests, handles approval buttons, and stores local audit data. Stdlib-only.
2. **`openclaw-gateway`** — isolated Codex CLI worker container. Handles source discovery and scoring-tuning agent work through the shared workspace. Codex auth lives in gitignored `openclaw/codex-home/`, not the host home directory.

The user interacts through Telegram. There is no web UI in `jobbot`. The bot never applies to jobs, messages recruiters, sends email, logs into LinkedIn, or mounts browser cookies.

## Mental Model

Everything is on-demand. There is no cron-driven collection.

| Entry Point | Behavior |
|---|---|
| `Get more jobs` | Rate-limited collection across enabled sources, cross-source dedupe, deterministic scoring, fresh digest of jobs not previously shown |
| `Update sources` | Writes a discovery request into `/jobbot/workspace/discovery`; waits for OpenClaw/Codex response; user approves sources in Telegram; approved rows are appended as `created_by: "agent", status: "test"` |
| `Tune scoring` | Writes a tuning request into `/jobbot/workspace/tuning`; shadow-tests proposed rules; user applies/rejects in Telegram |
| `Usage` | Shows OpenAI API spend and usage counters |

Two LLM tiers stay separate:

| Tier | Location | Use |
|---|---|---|
| Codex subscription | OpenClaw side | Source discovery and scoring-rule tuning |
| OpenAI API | `jobbot` | Cover notes only, behind local budget gates and per-day count caps |

Per-job scoring is deterministic and free. The agent updates rules; it does not score every job with an LLM.

## Source Of Truth

| Document | Use |
|---|---|
| [`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md) | Intended product behavior |
| [`tasks.md`](tasks.md) | Priority list and acceptance criteria |
| [`README.md`](README.md) | User setup and daily operation |
| `git log` | Recent implementation history |

## Repository Layout

```text
.
├── jobbot/
│   ├── __main__.py          # CLI entry
│   ├── app.py               # Thin-ish orchestration for Telegram/workspace/actions
│   ├── budget.py            # OpenAI spend gate
│   ├── config.py            # Settings, source/profile loading, safe path defaults
│   ├── coordinators.py      # Discovery/tuning file-contract writers and approval helpers
│   ├── database.py          # SQLite schema, migrations, dedupe, audit tables
│   ├── llm.py               # OpenAI cover-note client only
│   ├── logging_setup.py     # JSON logging + secret masking
│   ├── models.py            # Dataclasses
│   ├── scoring.py           # Deterministic rule interpreter
│   ├── sources.py           # Collectors + safe fetch/IMAP UID handling
│   └── telegram.py          # Telegram client, keyboards, callback parser
├── config/
│   ├── jobbot.json          # Budgets, model, rate limits
│   ├── profile.example.json # Safe structured profile template
│   ├── scoring.json         # Active scoring DSL
│   └── sources.json         # Source registry
├── input/
│   ├── profile.example.md   # Safe profile-description template
│   └── cv.example.md        # Safe CV-context template
├── openclaw/
│   └── JOB_SEARCH_AGENT_PROMPT.md
├── tests/
├── Dockerfile
├── docker-compose.yml
├── OPENCLAW_JOB_SEARCH_SPEC.md
├── tasks.md
├── README.md
├── AGENTS.md
└── CLAUDE.md
```

Private local files are ignored:

```text
.env
data/
config/profile.local.json
input/profile.local.md
input/cv.local.md
openclaw/config/
openclaw/workspace/
```

## Conventions

- **Python >= 3.9. Stdlib only.** Do not add dependencies without an explicit ask.
- Use `rg` for searching.
- Use `apply_patch` for manual edits.
- No comments unless the WHY is non-obvious.
- Telegram messages are plain text; do not introduce Markdown/HTML parsing casually.
- Word-boundary matching only for job-text rules.
- Never put secrets in URLs or logs.
- All cross-container IO is file-based in `/jobbot/workspace/{discovery,tuning}/`. No HTTP between containers and no shared SQLite.

## Build / Test / Run

```bash
python3 -m unittest discover -s tests
python3 -m pytest tests/ -q
python3 -m jobbot init
python3 -m jobbot collect
python3 -m jobbot digest
docker compose --profile openclaw config --quiet
```

If bytecode writes hit the sandbox:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache python3 -m unittest discover -s tests
```

Docker:

```bash
docker compose build jobbot
docker compose run --rm jobbot python -m jobbot init
docker compose up -d jobbot
docker compose --profile openclaw up -d openclaw-gateway
```

## Implemented Vs. Spec

| Area | Current State |
|---|---|
| Shared workspace | Implemented in compose and created by `jobbot` startup |
| On-demand collection | Implemented; `serve` polls Telegram/workspace only |
| Digest header buttons | Implemented: `Get more jobs`, `Update sources`, `Tune scoring`, `Usage` |
| Per-job buttons | Implemented: `Irrelevant`, `Remind me tomorrow`, `Give me cover note`, `Applied` |
| Cross-source dedupe | Implemented via canonical URL + normalized company/title |
| No re-spam | Implemented with `digest_log`, except snoozed jobs due for resend |
| Scoring DSL | Implemented in `config/scoring.json` + `jobbot/scoring.py` |
| Word-boundary matching | Implemented with tests for known false positives |
| Discovery request/approval | Implemented; automated worker writes OpenClaw/Codex response file |
| Tuning request/shadow/apply | Implemented; automated worker writes OpenClaw/Codex response file |
| OpenClaw worker runtime | Implemented in `openclaw/worker/` with Codex CLI |
| IMAP source filters | Implemented via per-source `query` and UID high-water |
| Source lifecycle | Implemented: `active`, `test`, `disabled`; `test` promotes on cover note/applied |
| Budget override | Implemented for cover notes |
| Structured logging | Implemented as JSON logs with secret masking |
| Docker hardening | Implemented for jobbot; OpenClaw has resource caps and dropped net admin/raw caps |

## Source And Scoring Files

Manual source schema:

```json
{
  "id": "unique-id",
  "name": "Display Name",
  "type": "rss",
  "url": "https://example.com/jobs.rss",
  "status": "active",
  "created_by": "user",
  "risk_level": "low",
  "headers": {},
  "query": null
}
```

Scoring rules belong in `config/scoring.json`, not hardcoded Python. The interpreter supports:

- `match_any_word`
- `match_all_word`
- `hard_reject_word`
- `field_equals`
- `numeric_at_least`
- `feedback_similarity`

## Non-Negotiables

- No logged-in LinkedIn/Wellfound browser automation. Email alerts only.
- No auto-apply, recruiter messaging, or outbound email.
- No browser profiles, host home, SSH keys, or `/var/run/docker.sock` mounts.
- No silent edits to `config/sources.json` or `config/scoring.json`; agent proposals require Telegram approval.
- No per-job LLM scoring.
- No cron/scheduler unless the user explicitly asks for the opt-in future mode.
- Do not commit `.env`, `data/`, local profile/CV files, generated drafts, API keys, or workspace request/response files.

## Ask Before Doing

- Adding any Python dependency.
- Implementing PDF/DOCX CV ingestion, because it conflicts with stdlib-only.
- Changing Codex worker auth or mounting strategy, because it can leak subscription credentials if done casually.
- Adding browser automation, even for public pages.

## Validation Before Declaring Done

```bash
python3 -m unittest discover -s tests
docker compose --profile openclaw config --quiet
git diff --check
git status -sb
```
