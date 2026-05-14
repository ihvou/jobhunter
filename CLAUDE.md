# Agents Guide

Orientation for AI coding agents and human contributors. Read this before the README. The README is user setup; this file is the implementation contract.

## Active OpenClaw Migration

This project has moved the user-facing bot runtime to real OpenClaw. See [`MIGRATION.md`](MIGRATION.md) for the phased migration record.

Current Phase 2 shape:

- `jobhunter-service` is the headless Python domain service.
- `openclaw-gateway` is the Dockerized real OpenClaw runtime and owns Telegram, Codex sessions, buttons, and channel I/O.
- Jobhunter tools are exposed through stdio MCP in [`jobhunter/openclaw_mcp.py`](jobhunter/openclaw_mcp.py) for Codex-native access and `mcp_tool_call_*` logs.
- [`plugins/jobhunter-tools/`](plugins/jobhunter-tools/) is an OpenClaw dynamic tool plugin that calls the same headless service over the Compose network so Jobhunter calls are trajectory-visible.
- Skills live under [`skills/`](skills/); rendering and routing rules that must always work belong in MCP tool descriptions first, with `SKILL.md` as duplicate guidance.
- The custom Node worker, Python Telegram client, and workspace file IPC are retired. Do not reintroduce `openclaw/worker/`, `jobhunter/telegram.py`, `jobhunter/agent.py`, or `openclaw/workspace/`.

## What This Project Is

A safe, low-cost job-search assistant that runs as two Docker containers:

1. **`jobhunter-service`**: stdlib Python service. Collects public/API/RSS/ATS/IMAP jobs, dedupes, scores, runs capped L2 relevance and cover-note calls, persists audits, and applies approved bounded actions.
2. **`openclaw-gateway`**: Dockerized OpenClaw gateway. Uses Codex via the user's subscription and reaches Jobhunter only through MCP. Codex auth is mounted read-only from `~/.codex`; no Docker socket is mounted.

The user interacts through Telegram via OpenClaw. The bot never applies to jobs, messages recruiters, sends email, logs into LinkedIn, or mounts browser cookies.

## Runtime Model

| Entry Point | Expected Tool Path |
|---|---|
| `Get more jobs` | `jobhunter_get_more_jobs`; if stale, `jobhunter_collect_all_sources`, then `jobhunter_get_more_jobs` again; render each job with `message` + `presentation.blocks[].buttons` |
| `Update sources` | OpenClaw/Codex investigates, calls `jobhunter_propose_actions` with `sources_proposal`; user approval calls `jobhunter_apply_action` |
| `Tune scoring` | Same as sources, using `scoring_rule_proposal` |
| `Usage` | `jobhunter_usage` |
| Inline `Applied` / `Irrelevant` / `Snooze` / `Cover` | Synthetic callback text routes to `jobhunter_mark_job` or `jobhunter_cover_note` using the 12-char `id_prefix` |
| `/history`, `/revert` | `jobhunter_history`, `jobhunter_revert_action` |

Two LLM tiers stay separate:

| Tier | Location | Use |
|---|---|---|
| Codex subscription | OpenClaw | Source discovery, strategy analysis, read-only data answers, scoring/filter tuning |
| OpenAI API | `jobhunter-service` | Cover notes and capped L2 relevance only, behind local budget gates |

L1 scoring is deterministic and free. L2 relevance is cached, budget-gated, and optional.

## Repository Layout

```text
jobhunter/
  __main__.py          # CLI entry
  agent_actions.py     # Bounded approval-gated action registry
  app.py               # Headless domain service core
  budget.py            # OpenAI spend gate
  config.py            # Settings, source/profile loading
  coordinators.py      # Scoring shadow-test helpers
  database.py          # SQLite schema, migrations, queries
  llm.py               # OpenAI cover-note + L2 relevance client
  openclaw_mcp.py      # MCP bridge used by OpenClaw/Codex
  scoring.py           # Deterministic scoring DSL interpreter
  service.py           # HTTP service for MCP tools
  sources.py           # Collectors + IMAP/email parser DSL
skills/
  jobhunter/
  leadhunter/
plugins/
  jobhunter-tools/
docker/openclaw-gateway/
bin/openclaw
```

Private local files are ignored:

```text
.env
data/
config/profile.local.json
config/sources.local.json
config/scoring.local.json
input/profile.local.md
input/cv.local.md
openclaw/config/
openclaw/codex-home/
```

## Conventions

- Python >= 3.9. Stdlib only. Do not add dependencies without an explicit ask.
- Use `rg` for searching.
- Use `apply_patch` for manual edits.
- Keep comments scarce and useful.
- Word-boundary matching only for job-text rules.
- Never put secrets in URLs or logs.
- OpenClaw/Codex must use MCP tools for Jobhunter data, not direct DB/file reads.
- `/agent` write actions must go through [`jobhunter/agent_actions.py`](jobhunter/agent_actions.py); never add an action kind that executes code or shell commands.
- Config-changing actions must be approval-gated and audited in `agent_actions`.

## Build / Test / Run

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache python3 -m unittest discover -s tests
python3 -m jobhunter init
python3 -m jobhunter collect
python3 -m jobhunter digest
docker compose --profile openclaw config --quiet
```

Docker:

```bash
./bin/openclaw start
./bin/openclaw onboard
./bin/openclaw status
./bin/openclaw logs
```

`./bin/jobhunter` is a deprecated wrapper for one release and delegates to `./bin/openclaw`.

## MCP / OpenClaw Non-Negotiables

- Do not remove `openclaw-gateway` from `docker-compose.yml`.
- `bin/openclaw onboard` must keep all three MCP registration steps:
  - OpenClaw config patch for `mcp.servers.jobhunter`
  - Codex home `[mcp_servers.jobhunter]` with `default_tools_approval_mode = "approve"`
  - `codex mcp add jobhunter -- python3 -m jobhunter.openclaw_mcp`
- OpenClaw tool policy is top-level `tools.*`, not `agents.defaults.tools.*`.
- `tools.alsoAllow` must include `jobhunter-tools`; do not use broad `group:plugins` for this bridge.
- Keep Codex app-server `approvalPolicy = "on-request"` and `sandbox = "read-only"`.
- Inline buttons render via `presentation.blocks[].buttons`.
- Verify agent behavior by trajectory/logs, not chat text. OpenClaw dynamic tools must appear as `tool.call` trajectory events; Codex-native MCP calls must appear as `mcp_tool_call_begin` / `mcp_tool_call_end` rows.

## Validation Before Declaring Done

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache python3 -m unittest discover -s tests
docker compose --profile openclaw config --quiet
git diff --check
git status -sb
```
