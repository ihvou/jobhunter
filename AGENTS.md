# Agents Guide

Orientation for AI coding agents (Claude Code, Codex, Cursor, etc.) and human contributors. Read this before the README. The README describes user setup; this file is the implementation contract.

## What This Project Is

A safe, low-cost job-search assistant that runs as two cooperating Docker containers:

1. **`jobhunter`** — deterministic Python service plus bounded OpenAI API calls for cover notes and L2 relevance. Collects public/API/RSS/email-alert jobs, scores them with `config/scoring.json`, sends Telegram digests, handles approval buttons, and stores local audit data. Stdlib-only.
2. **`openclaw-gateway`** — isolated Codex CLI worker container. Handles `/agent` strategy/data/source/filter work through the shared workspace. Codex auth lives in gitignored `openclaw/codex-home/`, not the host home directory.

The user interacts through Telegram. There is no web UI in `jobhunter`. The bot never applies to jobs, messages recruiters, sends email, logs into LinkedIn, or mounts browser cookies.

## Mental Model

Everything is on-demand. There is no cron-driven collection.

| Entry Point | Behavior |
|---|---|
| `Get more jobs` | Rate-limited collection across enabled sources, cross-source dedupe, deterministic L1 scoring, capped L2 relevance, fresh digest of jobs not previously shown |
| `Update sources` | Routes a canned `/agent` request; OpenClaw/Codex proposes `sources_proposal` actions; user approves in Telegram |
| `Tune scoring` | Routes a canned `/agent` request; OpenClaw/Codex proposes `scoring_rule_proposal` actions; user approves in Telegram |
| `Usage` | Replies with local spend/quota/recent-activity counters; does not queue Codex |
| `/agent <text>` | Writes an agent request into `/jobhunter/workspace/agent`; response may include `data_answer` plus bounded proposed actions |

Two LLM tiers stay separate:

| Tier | Location | Use |
|---|---|---|
| Codex subscription | OpenClaw side | Source discovery, strategy analysis, read-only data answers, and scoring/filter tuning |
| OpenAI API | `jobhunter` | Cover notes and capped L2 relevance only, behind local budget gates and per-day count caps |

L1 scoring is deterministic and free. L2 relevance is an optional, cached, budget-gated OpenAI API pass on top L1 candidates; Codex is not used to score every job.

## Source Of Truth

| Document | Use |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Intended product behavior |
| [`tasks.md`](tasks.md) | Priority list and acceptance criteria |
| [`README.md`](README.md) | User setup and daily operation |
| `git log` | Recent implementation history |

## Repository Layout

```text
.
├── jobhunter/
│   ├── __main__.py          # CLI entry
│   ├── agent.py             # /agent request/response workspace contract
│   ├── agent_actions.py     # Bounded proposed-action registry and handlers
│   ├── app.py               # Thin-ish orchestration for Telegram/workspace/actions
│   ├── budget.py            # OpenAI spend gate
│   ├── config.py            # Settings, source/profile loading, safe path defaults
│   ├── coordinators.py      # Discovery/tuning file-contract writers and approval helpers
│   ├── database.py          # SQLite schema, migrations, dedupe, audit tables
│   ├── llm.py               # OpenAI cover-note + L2 relevance client
│   ├── logging_setup.py     # JSON logging + secret masking
│   ├── models.py            # Dataclasses
│   ├── scoring.py           # Deterministic rule interpreter
│   ├── sources.py           # Collectors + safe fetch/IMAP UID handling
│   └── telegram.py          # Telegram client, keyboards, callback parser
├── config/
│   ├── jobhunter.json          # Budgets, model, rate limits
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
├── ARCHITECTURE.md
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
- All cross-container IO is file-based in `/jobhunter/workspace/{agent,discovery,tuning}/`. No HTTP between containers.
- `/agent` write actions must go through `jobhunter/agent_actions.py`; never add an action kind that executes code or shell commands.
- The OpenClaw worker tool surface is read-only: `query_sql`, `read_file`, `list_dir`, `http_fetch`, with caps and allowlists.

## Build / Test / Run

```bash
python3 -m unittest discover -s tests
python3 -m pytest tests/ -q
python3 -m jobhunter init
python3 -m jobhunter collect
python3 -m jobhunter digest
docker compose --profile openclaw config --quiet
```

If bytecode writes hit the sandbox:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache python3 -m unittest discover -s tests
```

Docker:

```bash
docker compose build jobhunter
docker compose run --rm jobhunter python -m jobhunter init
docker compose up -d jobhunter
docker compose --profile openclaw up -d openclaw-gateway
```

## Implemented Vs. Spec

| Area | Current State |
|---|---|
| Shared workspace | Implemented in compose and created by `jobhunter` startup |
| On-demand collection | Implemented; `serve` polls Telegram/workspace only |
| Reply keyboard controls | Implemented: `Get more jobs`, `Update sources`, `Tune scoring`, `Usage` |
| Per-job buttons | Implemented: `Irrelevant`, `Remind me tomorrow`, `Give me cover note`, `Applied` |
| Cross-source dedupe | Implemented via canonical URL + normalized company/title |
| No re-spam | Implemented with `digest_log`, except snoozed jobs due for resend |
| Scoring DSL | Implemented in `config/scoring.json` + `jobhunter/scoring.py` |
| Word-boundary matching | Implemented with tests for known false positives |
| Discovery request/approval | Implemented; automated worker writes OpenClaw/Codex response file |
| Tuning request/shadow/apply | Implemented; automated worker writes OpenClaw/Codex response file |
| OpenClaw worker runtime | Implemented in `openclaw/worker/` with Codex CLI |
| Agentic free-form loop | Implemented: `/agent` plus normal free-form text, shared `agent/` workspace, multi-action response schema |
| Agent action registry | Implemented with bounded handlers and audit/revert rows |
| L2 relevance pass | Implemented: cached OpenAI/API-or-local-fallback verdicts sorted into digest |
| Single profile file | Implemented: `input/profile.local.md` with `# About me` and `# Directives`; legacy JSON folds into it |
| Worker read-only tools | Implemented for agent loop with SELECT/file/path/http caps |
| IMAP source filters | Implemented via per-source `query` and UID high-water |
| Source lifecycle | Implemented: `active`, `test`, `disabled`; `test` promotes on cover note/applied |
| Budget override | Implemented for cover notes |
| Structured logging | Implemented as JSON logs with secret masking |
| Docker hardening | Implemented for jobhunter; OpenClaw has resource caps and dropped net admin/raw caps |

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
- No per-job Codex scoring. OpenAI L2 relevance is capped, cached, budget-gated, and optional.
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
