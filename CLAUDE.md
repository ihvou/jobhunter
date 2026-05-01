# Claude Instructions

Read [`AGENTS.md`](AGENTS.md) first — it is the canonical project contract.

## Quick orientation

- **On-demand only.** No cron, no background polling. Every action starts with a Telegram button click. Do not reintroduce a scheduler.
- **Two containers, file-based contract.** `jobbot` (Python, stdlib-only) ↔ `openclaw-gateway` (agent runtime) communicate via JSON files in `/jobbot/workspace/{discovery,tuning}/`. No HTTP between them, no shared SQLite.
- **Two LLM tiers, kept separate.** Codex (subscription) inside OpenClaw for source discovery + scoring tuning. OpenAI API (paid, budget-gated) inside jobbot, only for cover notes.
- **Per-job scoring is deterministic.** Rules live in `config/scoring.json` and are applied by a fixed interpreter. The LLM updates the rules; it does not score jobs directly.

## Source of truth

| Read this | For |
|---|---|
| [`OPENCLAW_JOB_SEARCH_SPEC.md`](OPENCLAW_JOB_SEARCH_SPEC.md) | The intended product |
| [`tasks.md`](tasks.md) | The honest gap list — every work item, prioritized |
| [`AGENTS.md`](AGENTS.md) | Repo layout, conventions, what's built vs. specified, how to extend |
| [`README.md`](README.md) | User docs (parts are stale; trust spec + tasks.md when in conflict) |

## Hard constraints

- No logged-in browser automation (LinkedIn, Wellfound, etc.). Email alerts only.
- No auto-apply. No recruiter messaging. No outbound email.
- No mounting browser cookies, host home, SSH keys, or `/var/run/docker.sock`.
- No silent edits to `config/sources.json` or `config/scoring.json` — agent-proposed changes go through a Telegram approval click.
- No new Python dependencies without an explicit ask. Stdlib-only is intentional.
- No comments unless the WHY is non-obvious. No multi-line docstrings.
- Word-boundary matching only for any rule applied to job text.
- Don't commit `.env`, `data/`, or anything with real CV/profile data.

## Validation before finishing

```bash
python3 -m unittest discover -s tests
docker compose --profile openclaw config --quiet
git diff --check
git status -sb
```

If Python bytecode compilation fails in the sandbox: `PYTHONPYCACHEPREFIX=/private/tmp/jobhunter_pycache`.
