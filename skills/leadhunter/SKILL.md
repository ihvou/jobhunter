---
name: leadhunter
description: Research lead sources and lead candidates from an ICP while preserving human approval before outreach.
metadata: { "openclaw": { "homepage": "https://github.com/ihvou/jobhunter" } }
---

Use this skill when the user asks for sales leads, founders, hiring managers, companies, contacts, or ICP research.

This is a planning/research skill until the lead backend endpoints land. Store no private contact data unless the user explicitly approves. Prefer public professional sources and cite evidence URLs.

Rules:

- Never automate logged-in LinkedIn browsing.
- Never send outreach automatically.
- Do not guess personal emails.
- Prefer public company sites, founder pages, conference speaker lists, GitHub orgs, newsletters, funding announcements, job posts, and compliant lead APIs.
- Return lead candidates with evidence, confidence, and contact surface. Make clear whether the data is public, inferred, or needs manual verification.
- Draft pitches only for user copy/paste.

Read `{baseDir}/prompts/lead_discovery.md` for source discovery strategy.
