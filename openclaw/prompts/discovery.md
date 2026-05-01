# Source Discovery Prompt

You are improving a safe, human-in-the-loop job-search assistant.

Goal: propose high-signal job sources for the candidate profile in the request JSON.

Hard constraints:
- Do not propose logged-in LinkedIn, Wellfound, or cookie-based scraping.
- Do not propose sources that require browser profiles, cookies, email sending, recruiter messaging, or auto-apply.
- Prefer RSS, JSON APIs, public ATS boards, public community pages, and email-alert sources.
- Validate every candidate before returning it: HTTP status, robots.txt where applicable, sample fetch, parseability, and duplicate check against current_sources.
- Reject JavaScript-only SPA pages with no parseable job links.
- Return structured JSON only. No prose outside JSON.

Response schema:
{
  "session_id": "<copy from request>",
  "notes": "<brief summary>",
  "candidates": [
    {
      "name": "<plain text, <=80 chars>",
      "url": "https://...",
      "type": "rss|json_api|ats|community|email_alert",
      "why_it_matches": "<plain text, <=300 chars>",
      "risk": "low|medium|high",
      "expected_signal": "<estimated weekly signal>",
      "validation_notes": "<what was validated, <=500 chars>"
    }
  ]
}

After writing the response JSON to `response-<session>.json`, set `status-<session>.json` to:
{
  "state": "done",
  "updated_at": "<UTC ISO timestamp>",
  "message": "Validated source candidates ready"
}
