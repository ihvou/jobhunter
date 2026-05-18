---
name: leadhunter
description: Research lead sources and lead candidates from an ICP while preserving human approval before outreach.
metadata: { "openclaw": { "homepage": "https://github.com/ihvou/jobhunter" } }
---

Use this skill when the user asks for sales leads, founders, hiring managers, companies, contacts, ICP research, or copy-paste outreach drafts.

The local service now has a small lead backend. Use the `leadhunter_*` tools from the `jobhunter-tools` OpenClaw plugin:

| User asks | Tool path |
|---|---|
| `/leads`, `Get leads`, `show lead digest` | `leadhunter_get_more_leads`; render each lead with Telegram inline buttons |
| `My ICP profile`, `show my lead ICP` | `leadhunter_show_icp`; reply with the current ICP markdown |
| `find me leads for <ICP>` | Research with `exa`/`firecrawl`/`web_search`, present candidates, then wait for approval before `leadhunter_save_leads` |
| `remember this source for leads` | `leadhunter_add_lead_source` after user approval |
| `shortlist/reject/archive this lead` or callback text | `leadhunter_mark_lead` |
| `Draft pitch` or `lead_pitch:<id_prefix>` | `leadhunter_draft_pitch`; send draft text only |

Rules:

- Never automate logged-in LinkedIn browsing.
- Never send outreach automatically.
- Do not guess personal emails.
- Do not save private contact data, cookie-derived data, or hidden profile data.
- Prefer public company sites, founder pages, conference speaker lists, GitHub orgs, newsletters, funding announcements, job posts, and compliant lead APIs.
- Return lead candidates with evidence, confidence, and contact surface. Make clear whether the data is public, inferred, or needs manual verification.
- Draft pitches only for user copy/paste.
- Save candidates only after explicit user approval.

Persistent reply keyboard:

```text
Get more jobs | My job profile
Get more leads | My ICP profile
```

Route `Get more leads` to `leadhunter_get_more_leads` and `My ICP profile` to `leadhunter_show_icp`. The other two buttons belong to the Jobhunter skill.

Callback routing:

```text
lead_shortlist:<12_hex> -> call leadhunter_mark_lead(id_prefix=<12_hex>, status="shortlisted")
lead_reject:<12_hex>    -> call leadhunter_mark_lead(id_prefix=<12_hex>, status="rejected")
lead_pitch:<12_hex>     -> call leadhunter_draft_pitch(id_prefix=<12_hex>) and reply with the draft
```

Read `{baseDir}/prompts/lead_discovery.md` for source discovery strategy and `{baseDir}/prompts/lead_agent.md` for the candidate approval flow.
