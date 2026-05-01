# Agent Instructions

This repository implements a safe, Docker-friendly job-search assistant for an OpenClaw-assisted workflow. The bot is an autonomous scout and drafter, not an autonomous applicant.

Follow these instructions for all future agent work in this repo.

## Product Contract

The system should:

- collect jobs from safe public sources, APIs, RSS feeds, company career pages, and optional scoped email alerts
- score jobs against a private local profile and preference file
- send ranked jobs to Telegram
- learn from Telegram feedback: `Irrelevant`, `Remind me tomorrow`, `Give me cover note`, `Applied`
- generate cover notes only after explicit user request
- keep costs low with rules, local filtering, cheap models, and budget gates
- keep all personal data, credentials, job history, drafts, and source metrics out of git

The system must not:

- automate logged-in LinkedIn, Wellfound, or other personal job-board sessions
- mount browser profiles, cookies, SSH keys, the host home directory, or Docker socket into containers
- submit applications automatically
- send recruiter messages or email
- enable new scraping sources without a human review step
- bypass the local LLM budget gate

## Safety Boundaries

Safety is enforced by capability design, not by prompt wording alone.

| Area | Required Behavior |
|---|---|
| LinkedIn | Email alerts are allowed; logged-in browser automation is forbidden |
| Browser profiles | Never mount or copy real browser profiles/cookies |
| Applications | User applies manually; bot may only track `Applied` after the user clicks it |
| Messaging | Telegram-to-user is allowed; recruiter/email/job-board messaging is forbidden |
| Source Discovery | Recommendation-only; never silently edit or enable `config/sources.json` |
| Email | Read-only scoped alert mailbox/folder only; no email send |
| Filesystem | Keep runtime data in `data/`; keep real profile in ignored local files |
| Cost | Check budget before LLM calls; prefer deterministic code before LLMs |

## Private Local Files

Committed files are examples only. Do not put real personal information into committed files.

| Committed Template | Private Local File | Purpose |
|---|---|---|
| `input/profile.example.md` | `input/profile.local.md` | Real CV/profile text |
| `config/profile.example.json` | `config/profile.local.json` | Role, skill, location, salary, and exclusion settings |
| `.env.example` | `.env` | API keys, Telegram token, IMAP credentials |

The local files above are ignored by git. If adding new private files, update `.gitignore`.

## Source Discovery Rules

`python3 -m jobbot discover-sources` should inspect:

- `input/profile.local.md`
- `config/profile.local.json` if present, otherwise `config/profile.example.json`
- source metrics from SQLite
- Telegram feedback history

It should then propose:

- public job boards and APIs
- ATS pages such as Ashby, Greenhouse, Lever, Workable, Teamtailor
- company lists and VC portfolio pages
- community sources such as Hacker News hiring threads
- safe search operators and query patterns

It must not auto-enable a source. Humans review recommendations, then manually edit `config/sources.json`.

## Repository Map

| Path | Purpose |
|---|---|
| `jobbot/` | Python standard-library MVP service |
| `jobbot/app.py` | Main orchestration: collect, digest, callbacks, source scoring |
| `jobbot/sources.py` | RSS/API/IMAP collectors and normalization |
| `jobbot/scoring.py` | Rule-based job scoring and hard rejects |
| `jobbot/telegram.py` | Telegram messages and inline callback parsing |
| `jobbot/llm.py` | OpenAI calls, cover notes, source-discovery prompts |
| `jobbot/budget.py` | Local cost estimates and budget gate |
| `jobbot/database.py` | SQLite schema and persistence |
| `config/sources.json` | Reviewed source registry |
| `config/jobbot.json` | Runtime settings and budget defaults |
| `docker-compose.yml` | `jobbot` service and optional `openclaw-gateway` profile |
| `OPENCLAW_JOB_SEARCH_SPEC.md` | Product/implementation spec |
| `README.md` | Operator guide |
| `docs/OPENCLAW_DOCKER_APPROVAL_STEPS.md` | Approval-gated account/service steps |

## Development Rules

- Keep the MVP dependency-light. The Python package currently uses the standard library only.
- Prefer deterministic parsing, filtering, and scoring before adding LLM calls.
- Preserve the Telegram button contract exactly: `Irrelevant`, `Remind me tomorrow`, `Give me cover note`, `Applied`.
- Do not introduce auto-apply, recruiter messaging, or logged-in browser automation.
- Do not write runtime data, credentials, real profile data, drafts, or SQLite files into git.
- Keep Docker volumes narrow and explicit.
- If adding a collector, support rate limits, clear source type, and safe default `enabled` behavior.
- If a source could be risky, add it disabled by default and document the risk.
- If changing LLM behavior, keep fallback behavior for no-API-key mode.
- If adding cost-bearing features, add budget-gate checks and tests.

## Validation Commands

Run these before finishing code changes:

```bash
python3 -m unittest discover -s tests
docker compose --profile openclaw config --quiet
git diff --check
```

Useful manual checks:

```bash
python3 -m jobbot init
python3 -m jobbot digest
python3 -m jobbot discover-sources
python3 -m jobbot usage
```

Use `PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache` if Python bytecode compilation tries to write outside the sandbox.

## Git Hygiene

- Do not commit `.env`, `data/`, `input/profile.local.md`, or `config/profile.local.json`.
- Check `git status -sb` before and after edits.
- Keep changes scoped to the user request.
- Do not rewrite history or reset user changes unless explicitly asked.

## References

- Product spec: `OPENCLAW_JOB_SEARCH_SPEC.md`
- Daily operator guide: `README.md`
- OpenClaw strategy prompt: `openclaw/JOB_SEARCH_AGENT_PROMPT.md`

