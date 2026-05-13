---
name: jobhunter
description: Operate Jobhunter through bounded MCP tools for job search, source discovery, scoring, and approved user actions.
metadata: { "openclaw": { "homepage": "https://github.com/ihvou/jobhunter" } }
---

You are using Jobhunter, a human-in-the-loop job search assistant.

Use this skill when the user asks about jobs, job sources, scoring, cover notes, history, or Jobhunter usage. The Python service is the source of truth. Do not edit Jobhunter files directly unless the user explicitly asks for code changes.

## Tool selection rules (read this first)

**Always go through the `jobhunter` MCP server. Never shell-grep the workspace, never `find`/`rg`/`sed` to locate files or read the SQLite DB.**

Decision tree for any user question:

| Question shape | Use this tool |
|---|---|
| "How many jobs / how many applied / show me top N" | `jobhunter_query_sql` with a SELECT |
| "Get fresh jobs / show me new matches" | `jobhunter_get_more_jobs` (uses cached digest, fast) |
| "Run collection / pull new jobs / refresh sources" | `jobhunter_collect_all_sources` (slow, ~30-60s) |
| "What's my spend / quota / usage" | `jobhunter_usage` |
| "What did I approve recently" | `jobhunter_history` |
| "Mark this job applied / irrelevant / snooze" | `jobhunter_mark_job` |
| "Write me a cover note" | `jobhunter_cover_note` |
| "Add this source / change scoring / tune profile" | `jobhunter_propose_actions` → user confirms → `jobhunter_apply_action` |
| "Undo last change" | `jobhunter_revert_action` |

If none of those fit, call `jobhunter_query_sql` with a SELECT against the `jobs`, `agent_actions`, `digest_log`, `job_feedback`, or `job_l2_verdicts` tables. The DB schema is the SoT. The skill file at `skills/jobhunter/SKILL.md` is descriptive only — do not grep it to answer user questions.

**Forbidden patterns when answering Jobhunter questions:**
- `find … *.sqlite`, `find … jobs`, or any `find`/`locate` on the workspace
- `rg`/`grep` against SKILL.md or any repo file
- `sed`/`cat`/`head` on workspace files
- Reading `jobs.sqlite` directly with `sqlite3` CLI (the service owns the DB)

Available MCP tools exposed by the `jobhunter` MCP server:

- `jobhunter_get_more_jobs`: return ranked job matches. Use `mark_sent=true` only after the jobs have been shown to the user.
- `jobhunter_collect_all_sources`: run collection/indexing from configured sources.
- `jobhunter_usage`: show local spend and quota counters.
- `jobhunter_history`: show recent approved/applied agent action rows.
- `jobhunter_propose_actions`: store bounded source/scoring/profile/email-parser actions for user approval.
- `jobhunter_apply_action`: apply one proposed action after explicit user approval.
- `jobhunter_revert_action`: revert a reversible action by audit id.
- `jobhunter_mark_job`: mark a job irrelevant, applied, or snoozed. Use only after explicit user intent.
- `jobhunter_cover_note`: draft a cover note for one job.
- `jobhunter_query_sql`: SELECT-only investigation against the local SQLite database.

Behavior rules:

- Never apply to jobs, message recruiters, send email, or automate logged-in LinkedIn.
- Do not invent job data. If you need current rows, call a Jobhunter tool — **not the filesystem**.
- Preserve the approval model: source/scoring/profile/email-parser changes must be passed to `jobhunter_propose_actions`, shown to the user with the returned action ids, and applied only after explicit user approval through `jobhunter_apply_action`.
- Prefer concise Telegram-friendly answers. Skip exploratory exposition; if the answer is a number, lead with the number.
- If `jobhunter_collect_all_sources` returns a timeout, do **not** retry — the collector continues in the background. Fall through to `jobhunter_get_more_jobs` with `mark_sent=true` to surface whatever finished ranking.
- For source discovery, prefer job aggregators, searchable boards, RSS/API/ATS feeds, and email alerts over arbitrary company pages.
- For relevance, prioritize product manager/product builder roles focused on Claude, Codex, AI agents, LLM tooling, workflow automation, or AI implementation.
- Reject Product Marketing Manager, MLOps, DevOps, pure engineering, and jobs requiring languages other than English, Ukrainian, or Russian unless the user overrides that.

Install notes live in `{baseDir}/README.md`.
