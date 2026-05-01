# Scoring Tuning Prompt

You are improving deterministic scoring rules for a safe job-search assistant.

Goal: propose an updated `config/scoring.json` ruleset from the request JSON.

Hard constraints:
- Output JSON only. No prose outside JSON.
- Use only the supported rule kinds: `match_any_word`, `match_all_word`, `hard_reject_word`, `field_equals`, `numeric_at_least`, `feedback_similarity`.
- Do not output code.
- Pattern matching must be word-boundary safe.
- Do not add broad description-level hard rejects that would reject senior roles for mentoring junior colleagues.
- Keep per-job scoring deterministic and free.
- Preserve or increment `version`; include `generated_by: "codex+openclaw"`.

Response schema: a complete scoring ruleset matching `config/scoring.json`.

After writing the response JSON to `response-<session>.json`, set `status-<session>.json` to:
{
  "state": "done",
  "updated_at": "<UTC ISO timestamp>",
  "message": "Scoring proposal ready for shadow test"
}
