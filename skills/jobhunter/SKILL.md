---
name: jobhunter
description: Operate Jobhunter through bounded OpenClaw plugin tools for job search, source discovery, scoring, and approved user actions.
metadata: { "openclaw": { "homepage": "https://github.com/ihvou/jobhunter" } }
---

You are using Jobhunter, a human-in-the-loop job search assistant.

Use this skill when the user asks about jobs, job sources, scoring, cover notes, history, or Jobhunter usage. The Python service is the source of truth. Do not edit Jobhunter files directly unless the user explicitly asks for code changes.

## Tool selection rules (read this first)

**Always go through the `jobhunter_*` OpenClaw plugin tools. Never shell-grep the workspace, never `find`/`rg`/`sed` to locate files or read the SQLite DB.**

Decision tree for any user question:

| Question shape | Use this tool |
|---|---|
| "How many jobs / how many applied / show me top N" | `jobhunter_query_sql` with a SELECT |
| "Get fresh jobs / show me new matches" | `jobhunter_get_more_jobs` (uses cached digest, fast) |
| "My job profile / show my job preferences" | `jobhunter_show_profile` |
| "Run collection / pull new jobs / refresh sources" | `jobhunter_collect_all_sources` (slow, ~30-60s) |
| "Rescore recent jobs / refresh scoring" | `jobhunter_rescore_recent_jobs` |
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

Available Jobhunter plugin tools:

- `jobhunter_get_more_jobs`: return ranked job matches. Use `mark_sent=true` only after the jobs have been shown to the user.
- `jobhunter_collect_all_sources`: run collection/indexing from configured sources.
- `jobhunter_rescore_recent_jobs`: rescore recent indexed jobs after feedback or profile/scoring changes.
- `jobhunter_usage`: show local spend and quota counters.
- `jobhunter_show_profile`: show the current `input/profile.local.md` job-search profile.
- `jobhunter_history`: show recent approved/applied agent action rows.
- `jobhunter_propose_actions`: store bounded source/scoring/profile/email-parser actions for user approval.
- `jobhunter_apply_action`: apply one proposed action after explicit user approval.
- `jobhunter_revert_action`: revert a reversible action by audit id.
- `jobhunter_mark_job`: mark a job irrelevant, applied, or snoozed. Accepts a full `job_id` or a 12-character `id_prefix` from inline callbacks. Use only after explicit user intent.
- `jobhunter_cover_note`: draft a cover note for one job. Accepts a full `job_id` or a 12-character `id_prefix` from inline callbacks.
- `jobhunter_query_sql`: SELECT-only investigation against the local SQLite database.
- `jobhunter_process_email`: ingest one parsed job-alert email from OpenClaw Gmail Pub/Sub/hooks or an email skill.

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

## Persistent reply keyboard

Keep the 2x2 Telegram reply keyboard visible on user-facing replies whenever the channel accepts it:

```text
Get more jobs | My job profile
Get more leads | My ICP profile
```

Route `Get more jobs` to `jobhunter_get_more_jobs`, `My job profile` to `jobhunter_show_profile`, `Get more leads` to `leadhunter_get_more_leads`, and `My ICP profile` to `leadhunter_show_icp`.

## Digest rendering with inline buttons

### Staleness self-heal (mandatory)

Before rendering any digest, inspect the response of `jobhunter_get_more_jobs`:

- `queue_freshness_hours` — hours since the last successful source collection
- `queue_is_stale` — true when freshness ≥ 6 hours

If stale, do this without asking:

1. Briefly tell the user: "Collecting fresh jobs, back in ~1 min."
2. Call `jobhunter_collect_all_sources` (pulls new Gmail alerts + RSS + ATS).
3. Call `jobhunter_get_more_jobs` again — now fresh.
4. Then render with inline buttons per below.

When the user asks for fresh jobs, for example "Get more jobs", after calling `jobhunter_get_more_jobs` you MUST emit the response via the OpenClaw `message` tool with per-job inline buttons. Do NOT just return a text reply.

For each job in the returned shortlist, use the two-call `messageId` workaround:

```text
1. Send the job card with placeholder buttons: pending:<job_id_first_12>.
2. Capture the returned messageId.
3. Immediately edit the same message with real callback_data:
   applied:<job_id_first_12>:<messageId>
   irrelevant:<job_id_first_12>:<messageId>
   snooze:<job_id_first_12>:<messageId>
   cover:<job_id_first_12>:<messageId>
```

`job_id_first_12` is the first 12 lowercase hex characters of the `jobs.id` value returned by `jobhunter_get_more_jobs`.

### Callback dispatch

When a user message arrives matching one of these patterns, treat it as a button-tap callback, not free-form text, and route immediately.

**Job callbacks:**

```text
applied:<12_hex>:<messageId>     -> jobhunter_mark_job(id_prefix=<12_hex>, status="applied")
irrelevant:<12_hex>:<messageId>  -> jobhunter_mark_job(id_prefix=<12_hex>, status="irrelevant")
snooze:<12_hex>:<messageId>      -> jobhunter_mark_job(id_prefix=<12_hex>, status="snoozed", snooze_days=1)
cover:<12_hex>:<messageId>       -> jobhunter_cover_note(id_prefix=<12_hex>)
```

**Lead callbacks:**

```text
lead_reached:<12_hex>:<messageId>     -> leadhunter_mark_lead(id_prefix=<12_hex>, status="reached_out")
lead_irrelevant:<12_hex>:<messageId>  -> leadhunter_mark_lead(id_prefix=<12_hex>, status="irrelevant")
lead_snooze:<12_hex>:<messageId>      -> leadhunter_mark_lead(id_prefix=<12_hex>, status="snoozed", snooze_days=7)
lead_pitch:<12_hex>:<messageId>       -> leadhunter_draft_pitch(id_prefix=<12_hex>)
```

The job/lead callback prefixes are deliberately distinct (`applied:` vs `lead_reached:`, `irrelevant:` vs `lead_irrelevant:`, `snooze:` vs `lead_snooze:`) so the dispatcher can route by prefix even if a job and a lead happen to share an `id_prefix`.

**After triage actions (applied/irrelevant/snooze on jobs; lead_reached/lead_irrelevant/lead_snooze on leads): delete the original card.**

```text
message({
  action: "delete",
  target: "telegram:<chat_id>",
  messageId: "<messageId_from_callback_data>"
})
```

The encoded `:<messageId>` is mandatory because OpenClaw 2026.5.7's synthetic callback prompt uses the callback_query id, not the button message id.

Snoozed items automatically reappear in the next `/jobs` or `/leads` digest after the snooze window expires (1 day for jobs, 7 days for leads). DB rows persist for audit.

**After draft actions (cover on jobs, lead_pitch on leads): send the draft as a NEW reply.**

```text
message({
  action: "send",
  target: "telegram:<chat_id>",
  message: "**Cover note draft:**\n<draft text>"      // or "**Pitch draft:**\n…"
})
```

Keep the original card for draft actions unless the user also tapped a triage action.
