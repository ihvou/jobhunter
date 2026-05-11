---
name: jobhunter
description: Operate Jobhunter through bounded MCP tools for job search, source discovery, scoring, and approved user actions.
metadata: { "openclaw": { "homepage": "https://github.com/ihvou/jobhunter" } }
---

You are using Jobhunter, a human-in-the-loop job search assistant.

Use this skill when the user asks about jobs, job sources, scoring, cover notes, history, or Jobhunter usage. The Python service is the source of truth. Do not edit Jobhunter files directly unless the user explicitly asks for code changes.

Available MCP tools are expected to be exposed by the `jobhunter` MCP server:

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
- Do not invent job data. If you need current rows, call a Jobhunter tool.
- Preserve the approval model: source/scoring/profile/email-parser changes must be passed to `jobhunter_propose_actions`, shown to the user with the returned action ids, and applied only after explicit user approval through `jobhunter_apply_action`.
- Prefer concise Telegram-friendly answers.
- For source discovery, prefer job aggregators, searchable boards, RSS/API/ATS feeds, and email alerts over arbitrary company pages.
- For relevance, prioritize product manager/product builder roles focused on Claude, Codex, AI agents, LLM tooling, workflow automation, or AI implementation.
- Reject Product Marketing Manager, MLOps, DevOps, pure engineering, and jobs requiring languages other than English, Ukrainian, or Russian unless the user overrides that.

Install notes live in `{baseDir}/README.md`.
