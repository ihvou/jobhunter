# OpenClaw Job Search Agent Specification

## 1. Executive Summary

This specification defines a Docker-isolated OpenClaw job-search agent that finds, ranks, and helps act on high-quality job opportunities without using LinkedIn browser automation or logged-in job-board scraping.

The agent runs as a user-driven scout and analyst, not an autonomous applicant. It searches when the user asks, refines its sources and scoring rules with the user's approval, sends a ranked Telegram digest, and waits for explicit human feedback before drafting cover notes or marking actions as complete.

Primary interaction happens through a Telegram bot. Top-level actions use a
persistent Telegram reply keyboard; per-job decisions stay inline:

| Button | Meaning | System Action |
|---|---|---|
| `Irrelevant` | This job is a bad fit | Down-rank similar jobs, log rejection reason if supplied |
| `Remind me tomorrow` | Interesting, but not now | Snooze job for 24 hours and resend |
| `Give me cover note` | Prepare an application note | Generate tailored cover note and optional CV bullet suggestions |
| `Applied` | User applied manually | Mark job as applied, update company/source success metrics |

Bot-level (digest-level) buttons:

| Button | Meaning | System Action |
|---|---|---|
| `Get more jobs` | Search for and show more jobs now | Run on-demand collection (§6.2), send fresh digest |
| `Update sources` | Refine the source list | OpenClaw + Codex propose new validated sources for user approval (§6.5) |
| `Tune scoring` | Refine the ranking rules | OpenClaw + Codex propose updated scoring rules; user reviews shadow-test before applying (§8.4) |

The design favors safety, low account-ban risk, and low LLM cost. Per-job
scoring is deterministic and free; the LLM is used only for source/scoring
updates (Codex via subscription) and cover notes (OpenAI API, paid).

## 2. Goals And Non-Goals

### 2.1 Goals

| Goal | Description | Priority |
|---|---|---:|
| Safe autonomous scouting | Search public, API, RSS, email-alert, and company-career-page sources without logged-in scraping | P0 |
| Dynamic source discovery | Identify new job sources, companies, platforms, search operators, and career pages based on the user's profile | P0 |
| Dynamic source optimization | Promote high-yield sources and deprioritize low-quality or duplicate-heavy sources | P0 |
| Telegram-first workflow | Send ranked opportunities and receive feedback through inline Telegram buttons | P0 |
| Human-in-the-loop applications | Never apply, message recruiters, or submit forms without explicit human action | P0 |
| Low cost | Prefer deterministic filtering, local embeddings, small models, and strict budget caps | P0 |
| Docker isolation | Run the full OpenClaw Gateway inside Docker with narrow mounted volumes and scoped secrets | P0 |
| Auditable decisions | Log source, score, reason, feedback, LLM usage, and all generated drafts | P1 |

### 2.2 Non-Goals

| Non-Goal | Reason |
|---|---|
| LinkedIn logged-in automation | High account-restriction risk and violates the desired safety posture |
| Auto-apply | Too risky for reputation, accuracy, and account safety |
| Auto-message recruiters | Outreach should remain user-approved |
| Broad host filesystem access | Docker isolation should prevent accidental or malicious file access |
| Expensive always-on reasoning | The agent should run bounded loops, not think continuously |

## 3. Key Assumptions

| Assumption | Impact |
|---|---|
| User can run Docker locally or on a VPS | Enables isolated OpenClaw Gateway |
| User can create a Telegram bot token | Enables interactive digest and feedback loop |
| User can provide CV and search preferences | Enables scoring and source selection |
| User may receive LinkedIn job alerts by email | Email parsing is allowed; logged-in LinkedIn browsing is not |
| OpenAI API key can be scoped to a dedicated project | Enables cost tracking and soft budget alerts |
| Hard budget enforcement is implemented in the agent | Provider-side monthly budgets may be soft alerts, not guaranteed hard caps |

## 4. Safety Model

### 4.1 Threats

| Threat | Example | Mitigation |
|---|---|---|
| Account restriction | Bot automates LinkedIn or Wellfound sessions | Do not mount browser profiles or cookies; use email alerts and public sources |
| Harmful outbound action | Agent applies to a job or messages recruiter | Deny external messaging except Telegram-to-user; no application submission tools |
| Secret leakage | CV/API keys exposed in prompts or logs | Narrow Docker volume; scoped API keys; redact logs |
| Runaway LLM cost | Agent loops on research or analyzes too many jobs | Local budget gate, daily caps, source caps, model allowlist |
| Host compromise | Agent runs shell commands on host | Full Gateway in Docker; no host home mount; avoid Docker socket mount |
| Source abuse | Agent aggressively crawls career pages | Rate limits, robots/ToS awareness, per-source fetch caps |

### 4.2 Tool Policy

| Tool Capability | Default Policy | Notes |
|---|---|---|
| RSS/API fetch | Allow | Primary collection mechanism |
| Public web fetch | Allow with rate limits | For company pages and job details |
| Browser automation | Deny by default | Optional later for non-logged-in pages only |
| Shell execution | Deny or tightly restricted | Prefer purpose-built scripts |
| Filesystem read/write | Allow only mounted workspace | No host home access |
| Telegram send | Allow only to configured user/chat | Only user-facing digest and notifications |
| Email read | Allow scoped mailbox/label only | Prefer dedicated mailbox or Gmail label |
| Email send | Deny | Avoid accidental outreach |
| Recruiter/job-board messaging | Deny | Human only |
| Application submission | Deny | Human only |

### 4.3 Docker Isolation Rules

| Rule | Requirement |
|---|---|
| No browser profile mount | Do not mount Chrome/Safari/Firefox profiles or cookies |
| No Docker socket mount | Do not mount `/var/run/docker.sock` into the OpenClaw container |
| Narrow volumes only | Mount only `/jobbot/data`, `/jobbot/config`, and optional `/jobbot/input` |
| Dedicated secrets | Use separate API keys/tokens only for this bot |
| Non-root runtime | Prefer non-root container user after setup |
| Network egress | Allow outbound HTTPS, but control behavior at application level |
| Logs | Persist logs to dedicated volume with rotation |

## 5. High-Level Architecture

```text
                         +-------------------+
                         |  User on Telegram |
                         +---------+---------+
                                   |
                                   v
                        +----------+-----------+
                        | Telegram Bot Channel |
                        +----------+-----------+
                                   |
                                   v
+----------------------- Docker Boundary ------------------------+
|                                                                 |
|  +-------------------+       +------------------------------+   |
|  | OpenClaw Gateway  |<----->| Job Search Agent Workspace   |   |
|  +---------+---------+       +---------------+--------------+   |
|            |                                 |                  |
|            v                                 v                  |
|  +-------------------+       +------------------------------+   |
|  | Source Collectors |       | SQLite / Logs / Drafts       |   |
|  +---------+---------+       +---------------+--------------+   |
|            |                                 |                  |
|            v                                 v                  |
|  +-------------------+       +------------------------------+   |
|  | Ranker / Analyzer |       | Budget + Usage Gate          |   |
|  +---------+---------+       +---------------+--------------+   |
|            |                                 |                  |
|            v                                 v                  |
|  +-------------------+       +------------------------------+   |
|  | Source Optimizer  |       | LLM Provider API             |   |
|  +-------------------+       +------------------------------+   |
|                                                                 |
+-----------------------------------------------------------------+
              |
              v
   Public RSS / APIs / Company Career Pages / Email Alerts
```

## 6. Core Scenarios

### 6.1 Scenario: Initial Setup

The primary input is a free-text job profile description (structured or
unstructured). A CV is optional and used only as secondary context for
cover-note generation.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Writes a job profile description into `input/profile.md` | `profile.md` |
| 2 | User | Optionally adds a text CV at `input/cv.md` for richer cover notes | `cv.md` (optional) |
| 3 | User | Starts Dockerized OpenClaw Gateway | Running gateway |
| 4 | Agent | Parses profile description | Normalized profile (target titles, role goals, strengths, exclusions) |
| 5 | Agent | Builds initial source list from `config/sources.json` seeds | Seeded sources |
| 6 | Agent | Sends Telegram setup summary with detected role goals | User confirms scope |

Example profile description (free text is acceptable; structure is not required):

```text
Product manager. Product lead. Product owner. Head of product. Product builder.
Product engineer. Goal is to create product prototypes, MVPs, or implement new
features in existing products via Claude-Code/Codex. Another option for the
role goal is implementing AI-based features or optimizing business processes
via AI-based automation.

Key strengths: product/feature discovery (done in both outsourcing and product
company environments), getting insights from product analytics, managing
multi-stakeholder environments.
```

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Profile parsed | Target titles, role goals, positive keywords, and exclusions extracted from `profile.md` |
| CV optional | If `cv.md` present, used for cover notes; if absent, the bot still scores and digests jobs |
| Telegram connected | Bot can send and receive callback actions |
| Safety policy active | No browser cookies, no auto-apply, no email-send permissions |

### 6.2 Scenario: On-Demand Job Collection

Job collection is triggered by the user, not by a fixed schedule. The Telegram
bot exposes a `Get more jobs` button. Clicking it runs a foreground collection
across all enabled sources, then sends a fresh digest of new (not-yet-shown)
jobs. A per-user rate limit prevents accidental hammering of sources.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Clicks `Get more jobs` in Telegram | Trigger event |
| 2 | Bot | Checks rate limit (default: 1 collection / 10 minutes) | Allow or "please wait Ns" reply |
| 3 | Bot | Replies "Searching for new jobs..." | TG ack |
| 4 | Collectors | Fetch RSS/API/email/career pages from enabled sources | Raw job candidates |
| 5 | Normalizer | Converts source-specific fields | Canonical job records |
| 6 | Dedupe | Removes duplicates within and across sources | Unique jobs |
| 7 | Rule Filter | Applies hard-reject rules from current scoring config (§8) | Candidate shortlist |
| 8 | Ranker | Scores jobs deterministically (§8) | Ranked jobs |
| 9 | Telegram Sender | Sends digest of jobs not previously shown to the user | New job cards |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| On-demand only | No cron-driven collection; first interaction is always a user click |
| Rate-limited | Repeated clicks within the rate window receive a short "wait" reply, not a fetch |
| Cross-source dedupe | Same company/title/canonical-URL not repeated across sources |
| No re-spam | A digest never contains a job already shown in a prior digest unless explicitly snoozed and now due |
| Hard filters respected | Excluded locations/sectors/levels are removed |
| Digest bounded | Max digest size is enforced |
| Responsive | Button click is acknowledged in <2s; full digest delivered within ~30s for typical source counts |

Note: a daily safety-net background fetch is intentionally out of scope. If
the digest pool feels stale between user clicks, add it later as an opt-in
`JOBBOT_DAILY_REFRESH=1` flag.

### 6.3 Scenario: Telegram Feedback Loop

| Button | User Intent | Immediate Action | Longer-Term Learning |
|---|---|---|---|
| `Irrelevant` | Bad fit | Mark job rejected | Lower weights for similar title/company/source/keywords |
| `Remind me tomorrow` | Revisit later | Snooze 24 hours | No negative signal |
| `Give me cover note` | Interested | Generate tailored note | Positive signal for source and job attributes |
| `Applied` | Application completed | Mark applied | Strong positive signal for source/company/type |

Optional follow-up prompts:

| Trigger | Follow-Up Question | Purpose |
|---|---|---|
| `Irrelevant` | "Why? role / location / salary / seniority / company / other" | Improve filter precision |
| `Applied` | "Where did you apply? company site / email / job board / other" | Track channel effectiveness |
| `Give me cover note` | "Tone? concise / warm / technical / founder-style" | Tailor draft output |

### 6.4 Scenario: Cover Note Generation

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Clicks `Give me cover note` | Draft request |
| 2 | Agent | Retrieves CV, job details, company notes | Context packet |
| 3 | Budget Gate | Checks daily/monthly budget | Allow/deny |
| 4 | LLM | Produces cover note | Draft |
| 5 | Agent | Sends note via Telegram and saves Markdown | User-ready draft |

Draft constraints:

| Constraint | Requirement |
|---|---|
| Accuracy | No invented experience |
| Length | 120-220 words by default |
| Style | Direct, specific, non-generic |
| Evidence | Reference 2-4 real matches from CV/job |
| Human action | User sends or submits manually |

### 6.5 Scenario: On-Demand Source Discovery (OpenClaw + Codex)

Source discovery is triggered by the user via the `Update sources` Telegram
button. The discovery work is performed by the OpenClaw Gateway acting as an
agent runtime, with Codex (via the user's subscription) as the LLM. OpenClaw
provides validation tools (HTTP fetch, robots.txt check, schema sniff) so
proposed sources are vetted, not hallucinated.

The two containers communicate through a shared workspace volume using a
file-based contract — no HTTP between containers, no shared database write
access. This keeps each container's blast radius narrow.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Clicks `Update sources` in Telegram | Trigger event |
| 2 | Bot | Writes `discovery/request-<ts>.json` to shared workspace | Discovery request |
| 3 | Bot | Replies "Discovery in progress, this can take 1-3 minutes" | TG ack |
| 4 | OpenClaw | Picks up new request file (file watch or short poll) | Agent session start |
| 5 | OpenClaw + Codex | Read profile, current sources, recent metrics; propose candidate sources | Initial candidate list |
| 6 | OpenClaw | For each candidate: HTTP HEAD, robots.txt check, sample fetch, schema sniff, dedupe vs current pool | Validated/rejected per candidate |
| 7 | OpenClaw + Codex | Iterate (e.g. "ATS X returns 403, find alternative"; "platform Y duplicates RemoteOK, drop") | Refined list |
| 8 | OpenClaw | Writes `discovery/response-<ts>.json` and sets `discovery/status-<ts>.json` to `done` | Response |
| 9 | Bot | Polls status file; on `done`, posts TG summary with `[Approve all][Approve N][Reject all]` buttons | Approval prompt |
| 10 | User | Approves desired candidates | Selection |
| 11 | Bot | Appends approved sources to `config/sources.json` with `created_by='agent'`, `enabled=true`, `status='test'` | Updated sources |

Communication contract (shared volume `./openclaw/workspace/discovery/`):

| File | Writer | Reader | Schema (top-level) |
|---|---|---|---|
| `request-<ts>.json` | jobbot | OpenClaw | `{profile_summary, current_sources, recent_metrics, instructions, max_candidates}` |
| `status-<ts>.json` | OpenClaw | jobbot | `{state: pending\|running\|done\|failed, updated_at, message}` |
| `response-<ts>.json` | OpenClaw | jobbot | `{candidates: [...], session_id, notes}` |

Each candidate in the response:

| Field | Description |
|---|---|
| `name` | Human-readable name |
| `url` | Endpoint or seed URL (validated to return 2xx and not be disallowed by robots.txt) |
| `type` | One of: `rss`, `json_api`, `ats`, `community`, `email_alert` |
| `why_it_matches` | 1-3 sentences referencing the user profile |
| `risk` | `low` / `medium` / `high` |
| `expected_signal` | Estimated weekly job count for the profile |
| `validation_notes` | What OpenClaw verified (status code, sample item count, etc.) |

Why Codex (vs the OpenAI API used for cover notes):

| Reason | Detail |
|---|---|
| Subscription cost | Discovery is rare (weekly at most); user's subscription is flat fee |
| Tool use | OpenClaw can drive Codex through an agent loop with web-fetch/validation tools, which the simpler `/v1/responses` API path does not |
| Risk isolation | OpenAI per-call API spend stays scoped to cover-note generation only |

Source-discovery prompt template (used by OpenClaw to brief Codex):

```text
Find high-signal job sources for this candidate profile.
Prioritize low-competition, fresh, direct-application, remote-compatible sources.
Avoid LinkedIn logged-in scraping or any source requiring cookie automation.
For each candidate, you must validate by attempting a sample fetch and parse.
Reject candidates that: return non-2xx, are disallowed by robots.txt, are
duplicates of the current source list, or require login/cookies.
Return refined candidates as structured JSON matching the response schema.
```

Source storage and provenance:

| Provenance | Location | Lifecycle |
|---|---|---|
| Manually added | `config/sources.json`, `created_by='user'` | User edits directly; never modified by the agent |
| Agent-discovered | `config/sources.json`, `created_by='agent'`, `status='test'` initially | Appended only after user approval; user can disable; the agent does not silently mutate existing entries |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| On-demand only | No cron-driven discovery |
| Validated candidates | Every approved candidate has been fetched at least once successfully by OpenClaw before the user sees it |
| User approval gate | Sources are not added to `sources.json` without an explicit Telegram approval click |
| Provenance preserved | Manual and agent-discovered sources are visually distinguishable in `sources.json` |
| Subscription-only LLM cost | Discovery uses Codex via subscription; OpenAI per-call API is not invoked |

## 7. Source Strategy

### 7.1 Initial Source Categories

| Source Category | Examples | Access Method | Risk | Priority |
|---|---|---|---|---:|
| Remote RSS feeds | We Work Remotely, Remotive, Real Work From Anywhere | RSS | Low | P0 |
| Public job APIs | Remotive, Arbeitnow, Adzuna, RemoteOK JSON | API/JSON | Low | P0 |
| ATS career pages | Ashby, Greenhouse, Lever, Workable, Teamtailor | Public fetch | Low-medium | P0 |
| Startup ecosystems | YC Work at a Startup, startup.jobs, VC portfolio pages | Public fetch/search | Low-medium | P1 |
| Email alerts | LinkedIn alerts, Wellfound alerts, Google alerts, company alerts | Email read only | Low | P1 |
| Community threads | Hacker News Who's Hiring, selected forums | Public fetch | Low | P1 |
| Company lists | AI/devtools/infra/fintech target companies | Public web | Low | P1 |
| Funding signals | VC blogs, launch posts, accelerator batches | Public web | Low | P2 |

### 7.2 Search Operators And Patterns

| Pattern | Purpose | Example |
|---|---|---|
| ATS domain search | Discover direct job posts | `site:jobs.ashbyhq.com "founding engineer" "remote"` |
| Company stack search | Find companies using relevant tech | `"we're hiring" "TypeScript" "LLM" "remote"` |
| Founder hiring search | Find early-stage posts | `"founding engineer" "apply" "remote"` |
| Funding-to-hiring search | Find fresh hiring budgets | `"raised seed" "hiring engineers" "AI"` |
| Region/timezone search | Match availability | `"remote" "Europe" "Asia timezone" "senior engineer"` |

### 7.3 Source Lifecycle

With on-demand collection (§6.2), there is no polling-frequency tier — every
collection click fetches every active source. Each source carries a simple
lifecycle status instead:

| Status | Meaning | Included in `Get more jobs`? |
|---|---|---|
| `active` | User-managed source, or agent-discovered source promoted by the user | Yes |
| `test` | Newly added by `Update sources` agent flow (§6.5); on probation | Yes, but flagged in the digest as "from a new source" |
| `disabled` | Skipped by collection; preserved in `sources.json` for history | No |

Promotion `test → active` happens implicitly the first time the user clicks
`Applied` or `Give me cover note` on a job from that source. Demotion to
`disabled` is a manual user action (`config/sources.json` edit) or an agent
recommendation surfaced through the next `Update sources` run.

## 8. Job Scoring

Per-job scoring is fully deterministic and free at runtime — no LLM call per
job. The scoring algorithm itself is generated and periodically refined by
OpenClaw + Codex, using the user's accumulated feedback as the training
signal. Algorithm updates are gated by user approval.

### 8.1 Two-Layer Architecture

| Layer | Runs | Cost per job | Updated by |
|---|---|---|---|
| Scoring rules (`config/scoring.json`) | Once per algorithm update | n/a | OpenClaw + Codex on demand (`Tune scoring` button) |
| Rule interpreter (`jobbot/scoring.py`) | On every job | Free; deterministic | Code change (versioned in git) |

The interpreter exposes a fixed set of rule kinds (§8.3). Codex can only
output rules in this DSL; it cannot inject arbitrary code. This keeps the
"agent updates the scorer" idea safe to operate.

### 8.2 Scoring Rules File

```json
{
  "version": 3,
  "generated_at": "2026-05-01T...Z",
  "generated_by": "codex+openclaw",
  "previous_version": 2,
  "rules": [
    {
      "id": "title_product_role",
      "kind": "match_any_word",
      "fields": ["title"],
      "patterns": ["product manager", "head of product", "product owner", "product engineer"],
      "weight": 20
    },
    {
      "id": "ai_focus",
      "kind": "match_any_word",
      "fields": ["title", "description"],
      "patterns": ["llm", "ai automation", "claude", "codex", "agents"],
      "weight": 15
    },
    {
      "id": "remote_friendly",
      "kind": "field_equals",
      "field": "remote_policy",
      "value": "remote",
      "weight": 10
    },
    {
      "id": "exclude_seniority",
      "kind": "hard_reject_word",
      "fields": ["title", "description"],
      "patterns": ["intern", "internship"]
    },
    {
      "id": "exclude_industries",
      "kind": "hard_reject_word",
      "fields": ["title", "description", "company"],
      "patterns": ["gambling", "casino", "adult", "defense", "weapons"]
    }
  ],
  "thresholds": {
    "min_show_score": 50,
    "hard_reject_floor": 0
  },
  "fallback": "baseline_v0"
}
```

### 8.3 Supported Rule Kinds

| Kind | Effect |
|---|---|
| `match_any_word` | Word-boundary match of any pattern in any listed field; awards `weight` if any match |
| `match_all_word` | All patterns must match across the listed fields; awards `weight` if all match |
| `hard_reject_word` | Word-boundary match → `hard_reject = true` |
| `field_equals` | Equality on a normalized field (e.g. `remote_policy == "remote"`); awards `weight` |
| `numeric_at_least` | Numeric field (e.g. `salary_max`) `>=` threshold; awards `weight`; optional `hard_reject_below` |
| `feedback_similarity` | Token-overlap (or local-embedding) similarity to past `applied`/`cover_note` jobs; awards a fraction of `weight` |

Pattern matching is always **word-boundary** to prevent the false-positive
class (e.g. `intern` matching `international`). Codex receives this constraint
as part of its briefing.

### 8.4 Algorithm Update Flow

| Step | Actor | Action |
|---:|---|---|
| 1 | User | Clicks `Tune scoring` in Telegram |
| 2 | Bot | Writes `tuning/request-<ts>.json` with profile, current rules, recent feedback aggregates, score-distribution stats, sample of recent flagged jobs |
| 3 | OpenClaw + Codex | Propose updated rules conforming to the §8.3 schema |
| 4 | OpenClaw | Schema-validate proposed rules; reject if invalid |
| 5 | Bot | Shadow-tests proposed rules against the last N=100 jobs and reports: distribution shift, agreement rate vs Applied/Irrelevant feedback, false-reject rate against historical Applied jobs |
| 6 | Bot | Sends TG summary with diff and shadow-test report; offers `[Apply][Reject][Show diff]` |
| 7 | User | Approves or rejects |
| 8 | Bot | On Apply: archive previous `scoring.json` to `scoring.v<previous>.json`; activate new version; record entry in `scoring_versions` table |

The shadow test is the safety net. The user never activates a ruleset they
haven't seen the impact of.

### 8.5 Hard Filters

Hard filters are now expressed as `hard_reject_word` (or `numeric_at_least`
with `hard_reject_below`) rules in the scoring file, so they can be tuned by
the same mechanism. Examples the initial baseline file should include:

| Filter | Example rule |
|---|---|
| Excluded domains | `hard_reject_word` over `["gambling","casino","adult","defense","weapons"]` |
| Location mismatch | `hard_reject_word` over `["us only","u.s. only","security clearance"]` |
| Seniority mismatch | `hard_reject_word` over `["intern","internship","graduate program"]` |
| Compensation mismatch | `numeric_at_least` on `salary_max` with `hard_reject_below: <floor>` |
| Duplicates | Enforced upstream by dedupe (§6.2), not via scoring rules |

### 8.6 Output Explanation

Each Telegram job card includes:

| Field | Description |
|---|---|
| Title | Job title |
| Company | Company name |
| Score | 0-100 |
| Location | Remote/region constraints |
| Source | Where it was found |
| Why it matches | List of rule IDs that fired positively, with the matched pattern |
| Concerns | List of rule IDs that produced soft penalties or unmet conditions |
| Link | Source URL |
| Buttons | Inline feedback buttons |

Reasons and concerns are derived from which rules fired, so the user can see
exactly which rule promoted or demoted a job — and can ask the agent to
adjust that rule on the next tuning cycle.

### 8.7 Cost Profile

- Per-job scoring: 0 LLM calls.
- Per-discovery cycle (weekly at most): Codex via subscription.
- Per-tuning cycle (on-demand only, via `Tune scoring`): Codex via subscription.
- Per-cover-note (on-demand): OpenAI API (paid; see §11).

The OpenAI API budget gate in §11 only needs to govern cover-note drafts. The
rest of the system runs at zero per-call LLM cost.

## 9. Telegram Bot Design

### 9.1 Digest Message Format

A digest is sent in response to `Get more jobs`. The persistent reply keyboard
stays available for bot-level controls; the digest itself contains a header and
then one card per job. Cards are only emitted for jobs not already shown to the
user in a previous digest (or snoozed-and-now-due).

```text
New job matches

Reply keyboard: [Get more jobs] [Update sources] [Tune scoring] [Usage]

1. Senior AI Product Engineer - ExampleCo
Score: 91
Source: Ashby career page
Location: Remote, Europe overlap

Why it matches:
- title_product_role: matched "product engineer"
- ai_focus: matched "llm"
- remote_friendly

Concern:
- compensation_disclosed: salary not listed

[Irrelevant] [Remind me tomorrow] [Give me cover note] [Applied]
```

### 9.2 Telegram Actions And Callback Payloads

Top-level reply keyboard messages:

| Button | Parsed Action | Behavior |
|---|---|---|
| `Get more jobs` | `bot:collect` | Trigger on-demand collection (§6.2), respect rate limit |
| `Update sources` | `bot:discover_sources` | Open OpenClaw discovery request (§6.5) |
| `Tune scoring` | `bot:tune_scoring` | Open OpenClaw scoring-tune request (§8.4) |
| `Usage` | `bot:usage` | Reply with daily/monthly OpenAI spend and counts |

Slash-command fallbacks:

| Command | Parsed Action |
|---|---|
| `/jobs` | `bot:collect` |
| `/sources` | `bot:discover_sources` |
| `/tune` | `bot:tune_scoring` |
| `/usage` | `bot:usage` |

Per-job buttons:

| Button | Callback Data | Required Fields |
|---|---|---|
| `Irrelevant` | `job:irrelevant:<job_id>` | `job_id`, `user_id`, timestamp |
| `Remind me tomorrow` | `job:snooze_1d:<job_id>` | `job_id`, snooze_until |
| `Give me cover note` | `job:cover_note:<job_id>` | `job_id`, draft_request_id |
| `Applied` | `job:applied:<job_id>` | `job_id`, applied_at |

Approval inline buttons:

| Button | Callback Data | Behavior |
|---|---|---|
| `Approve discovery N` | `disc:approve:<session_id>:<idx>` | Append a discovered source to `sources.json` |
| `Reject discovery` | `disc:reject:<session_id>` | Drop the discovery proposal |
| `Apply scoring` | `tune:apply:<session_id>` | Activate the proposed scoring rules |
| `Reject scoring` | `tune:reject:<session_id>` | Drop the proposed scoring rules |

### 9.3 Telegram State Transitions

| Current State | Button | New State |
|---|---|---|
| `new` | `Irrelevant` | `rejected` |
| `new` | `Remind me tomorrow` | `snoozed` |
| `new` | `Give me cover note` | `draft_requested` |
| `new` | `Applied` | `applied` |
| `snoozed` | digest resend | `new` |
| `draft_requested` | draft generated | `draft_ready` |
| `draft_ready` | `Applied` | `applied` |
| `draft_ready` | `Irrelevant` | `rejected` |

### 9.4 Feedback Learning

| Feedback | Learning Update |
|---|---|
| `Irrelevant` | Negative weight for title keywords, source, company type, location, and semantic cluster |
| `Remind me tomorrow` | Neutral signal; preserve priority for next digest |
| `Give me cover note` | Positive interest signal for source and semantic cluster |
| `Applied` | Strong positive action signal for source, company type, role type, and query pattern |

## 10. Data Model

### 10.1 Tables

| Table | Purpose |
|---|---|
| `candidate_profile` | Normalized profile from `profile.md` (and optional `cv.md`) |
| `sources` | Source registry; provenance via `created_by` |
| `source_runs` | Fetch attempts, counts, errors, and cost |
| `jobs` | Canonical job records |
| `job_scores` | Per-job score and which rule IDs fired |
| `job_feedback` | Telegram button feedback |
| `drafts` | Cover notes and CV suggestions |
| `usage_log` | OpenAI per-call token and cost records (cover notes) |
| `discovery_runs` | Source-discovery sessions: request, status, response file paths, candidate counts, approval outcome |
| `scoring_versions` | History of `scoring.json`: version, generated_by, activated_at, shadow-test report |
| `digest_log` | Per-digest record: digest_id, timestamp, job_ids included; supports the "no re-spam" guarantee |
| `rate_limits` | Per-action throttle state (e.g. last `bot:collect` timestamp) |

### 10.2 `jobs` Fields

| Field | Type | Description |
|---|---|---|
| `id` | text | Stable internal ID |
| `source_id` | text | Source registry ID |
| `external_id` | text | Source-provided ID if available |
| `url` | text | Canonical URL |
| `title` | text | Job title |
| `company` | text | Company name |
| `location` | text | Raw location |
| `remote_policy` | text | Remote/hybrid/onsite/unknown |
| `salary_min` | integer | Optional |
| `salary_max` | integer | Optional |
| `currency` | text | Optional |
| `description` | text | Cleaned description |
| `posted_at` | datetime | Source posting time |
| `first_seen_at` | datetime | First collected time |
| `last_seen_at` | datetime | Last collected time |
| `status` | text | `new`, `snoozed`, `rejected`, `draft_ready`, `applied` |

### 10.3 `sources` Fields

| Field | Type | Description |
|---|---|---|
| `id` | text | Source ID |
| `name` | text | Human name |
| `type` | text | RSS/API/ATS/email/search/community |
| `url` | text | Endpoint or seed URL |
| `risk_level` | text | low/medium/high |
| `poll_frequency_minutes` | integer | Current polling interval |
| `enabled` | boolean | Active flag |
| `score` | integer | 0-100 source score |
| `last_run_at` | datetime | Latest run |
| `created_by` | text | seed/user/agent |

## 11. LLM Usage Strategy

### 11.1 Model Routing

The two LLM tiers serve clearly separated purposes. Most tasks use Codex via
the user's flat-fee subscription; only cover-note generation calls the
metered OpenAI API.

| Task | Method | Engine | Frequency |
|---|---|---|---|
| Fetch and parse RSS/API | Deterministic code | None | Per click |
| Dedupe | Hashing and rules | None | Per job |
| Hard filtering | Rules from `scoring.json` | None | Per job |
| Per-job scoring | Rule interpreter (§8) | None | Per job |
| Source discovery | Agent loop (OpenClaw + LLM) | Codex (subscription) | On demand, weekly at most |
| Scoring-rules tuning | Agent loop (OpenClaw + LLM) | Codex (subscription) | On demand only |
| Embedding similarity | Local embedding model | Local | Optional, per job |
| Cover note | Single prompt | OpenAI API (paid) | On demand, per click |
| CV bullet suggestions (Phase 3) | Single prompt | OpenAI API (paid) | On demand |

### 11.2 Budget Rules

Subscription-based (Codex) work has no per-call budget; throttling comes from
user clicks. Only OpenAI-API calls (cover notes) are budget-gated.

| Budget | Default | Applies To |
|---|---:|---|
| Daily OpenAI budget | `$0.50` | Cover notes |
| Monthly OpenAI budget | `$10.00` | Cover notes |
| Max cover-note drafts per day | `10` | Cover notes |
| Max source-discovery runs per day | `3` | Codex (anti-abuse, not cost) |
| Max scoring-tune runs per day | `3` | Codex (anti-abuse, not cost) |
| Per-action rate limit | `1 / 10 min` | `Get more jobs` |

Budget gate behavior (cover notes):

| Condition | Behavior |
|---|---|
| Under budget | Allow OpenAI call |
| Daily budget exceeded | Telegram prompt: "Daily budget exceeded. [Override once] [Cancel]" |
| Monthly budget exceeded | Telegram prompt: "Monthly budget exceeded. [Override once] [Cancel]" |

### 11.3 Cost Visibility

| Surface | Content |
|---|---|
| Telegram reply keyboard | "Usage" button replies with: jobs collected today, OpenAI spent today, OpenAI spent this month, cover-notes today, last discovery, last scoring update |
| SQLite `usage_log` | Per-OpenAI-call token and cost record |
| SQLite `discovery_runs` | One row per discovery session (no per-call cost; subscription) |
| SQLite `scoring_versions` | One row per scoring update (no per-call cost; subscription) |

## 12. OpenClaw Docker Deployment

### 12.1 Volumes

The shared workspace volume is the **only** path both containers can see.
Everything else is private to one container.

| Host Path | jobbot container | OpenClaw container | Purpose |
|---|---|---|---|
| `./data` | `/jobbot/data` rw | — (not mounted) | SQLite, logs, drafts |
| `./input` | `/jobbot/input` ro | — (not mounted) | `profile.md`, optional `cv.md` |
| `./config` | `/jobbot/config` rw | — (not mounted) | `sources.json`, `scoring.json`, `jobbot.json` |
| `./openclaw/workspace` | `/jobbot/workspace` rw | `/openclaw/workspace` rw | Discovery & tuning request/response files |
| `./openclaw/config` | — (not mounted) | `/openclaw/config` rw | OpenClaw's own state |

Note that **jobbot writes its DB and config files only to its own private
volumes**; OpenClaw cannot read or modify them directly. All cross-container
work happens through the shared `workspace/` volume via the JSON file
contracts in §6.5 and §8.4.

Do not mount:

| Path | Reason |
|---|---|
| Host home directory | Too broad |
| Browser profiles | Account-ban and cookie theft risk |
| SSH keys | Not needed |
| Docker socket | Container escape risk |

### 12.2 Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | yes, if using OpenAI API | LLM calls |
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot |
| `TELEGRAM_ALLOWED_CHAT_ID` | yes | Restrict recipient |
| `JOBBOT_DAILY_BUDGET_USD` | yes | App-level hard budget |
| `JOBBOT_MONTHLY_BUDGET_USD` | yes | App-level hard budget |
| `GMAIL_CLIENT_ID` | optional | Gmail alert reader |
| `GMAIL_CLIENT_SECRET` | optional | Gmail alert reader |
| `EMAIL_IMAP_URL` | optional | IMAP alert reader |

### 12.3 Example Compose Skeleton

```yaml
services:
  openclaw-jobbot:
    image: ghcr.io/openclaw/openclaw:latest
    container_name: openclaw-jobbot
    restart: unless-stopped
    ports:
      - "127.0.0.1:18789:18789"
    environment:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
      TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}"
      TELEGRAM_ALLOWED_CHAT_ID: "${TELEGRAM_ALLOWED_CHAT_ID}"
      JOBBOT_DAILY_BUDGET_USD: "0.50"
      JOBBOT_MONTHLY_BUDGET_USD: "10.00"
    volumes:
      - ./data:/jobbot/data
      - ./input:/jobbot/input:ro
      - ./config:/jobbot/config
```

This is a skeleton, not final production compose. The final compose should match the exact OpenClaw image, command, state directory, and channel setup used in the installed version.

## 13. Email Alert Handling

### 13.1 Supported Email Patterns

| Source | Handling |
|---|---|
| LinkedIn job alerts | Parse email only; do not open logged-in LinkedIn session |
| Wellfound alerts | Parse email only; do not automate account |
| Google alerts | Parse linked public pages |
| Company alerts | Parse direct company links |
| Job-board alerts | Prefer RSS/API equivalent if available |

### 13.2 Email Safety Rules

| Rule | Requirement |
|---|---|
| Read-only mailbox | No email sending |
| Label/folder scoped | Read only `job-alerts` label/folder |
| Link handling | Open only public/direct job links automatically |
| Logged-in links | Send link to user, do not attempt login |
| PII handling | Do not forward full CV unless user asks |

## 14. Implementation Phases

### Phase 1: On-Demand MVP

| Feature | Included |
|---|---|
| Dockerized OpenClaw Gateway with shared workspace volume | Yes |
| Telegram reply keyboard (`Get more jobs`, `Update sources`, `Tune scoring`, `Usage`) plus per-job inline buttons | Yes |
| Profile description parsing (CV optional) | Yes |
| RSS/API collectors | Yes |
| SQLite dedupe/logging with cross-source dedupe and digest_log | Yes |
| `scoring.json` baseline ruleset + interpreter (deterministic, no LLM per job) | Yes |
| Per-job feedback buttons | Yes |
| Cover note generation via OpenAI API (paid, budget-gated) | Yes |
| Auto-apply | No |
| Browser automation | No |
| Cron-driven collection | No (replaced by on-demand) |

### Phase 2: Agent-Driven Source Discovery

| Feature | Included |
|---|---|
| `Update sources` flow: jobbot ↔ OpenClaw ↔ Codex via shared workspace | Yes |
| Per-candidate validation by OpenClaw (HTTP HEAD, robots.txt, sample fetch) | Yes |
| Telegram approval gate; agent-discovered sources written to `sources.json` with `created_by='agent'` | Yes |
| `discovery_runs` table for audit | Yes |

### Phase 3: Agent-Driven Scoring Tuning

| Feature | Included |
|---|---|
| `Tune scoring` flow: jobbot ↔ OpenClaw ↔ Codex via shared workspace | Yes |
| Schema-validated rules output (DSL only, no codegen) | Yes |
| Shadow test against last N=100 jobs before activation | Yes |
| Telegram approval gate with diff and shadow-test report | Yes |
| `scoring_versions` table for audit and rollback | Yes |

### Phase 4: Application Assistance

| Feature | Included |
|---|---|
| Cover note generation | Yes (in Phase 1) |
| CV bullet suggestions | Yes |
| Company research summary | Yes |
| Application checklist | Yes |
| Manual apply tracking | Yes |
| Auto-submit | No |

### Phase 5: Optional Advanced Features

| Feature | Notes |
|---|---|
| Browser preview for public pages | Only non-logged-in pages |
| Dedicated web dashboard | Optional; Telegram remains primary |
| Multi-profile variants | Different `profile.md` per role cluster |
| CV ingestion in any format | Accept `cv.pdf`, `cv.docx`, `cv.doc`, or a public URL pointing to the CV; extract to text on `init`. Today only `cv.md` is read |
| Interview follow-up tracking | Later workflow |
| Recruiter outreach drafts | Draft only; user sends manually |
| Daily safety-net background collection (`JOBBOT_DAILY_REFRESH=1`) | Opt-in only |

## 15. Acceptance Criteria

| Area | Criteria |
|---|---|
| Safety | No LinkedIn cookies, no auto-apply, no outbound recruiter messaging |
| Docker | Gateway and jobbot run in Docker; jobbot ↔ OpenClaw share only `./openclaw/workspace/` |
| Telegram per-job | Digest and four required per-job buttons work |
| Telegram bot-level | `Get more jobs`, `Update sources`, `Tune scoring`, `Usage` buttons work |
| On-demand collection | No cron; rate-limited; cross-source dedupe; never re-shows previously digested jobs |
| Source discovery | Triggered on demand; OpenClaw + Codex iterate with validation; user approves per-candidate; written to `sources.json` with `created_by='agent'` |
| Scoring tuning | Triggered on demand; OpenClaw + Codex propose rules in the §8.3 DSL; shadow-tested; user approves before activation; previous version archived |
| Per-job scoring | Fully deterministic; zero LLM calls per job; word-boundary matching only |
| Cost | OpenAI API used only for cover notes; daily/monthly OpenAI budget gate blocks excess; Codex use is subscription-bounded |
| Audit | Every job, score, feedback action, OpenAI call, discovery run, and scoring version is logged |

## 16. Open Questions

| Question | Default Assumption |
|---|---|
| Preferred roles | Extract from `profile.md`, then ask user to confirm in Telegram |
| Salary floor | Ask user during setup |
| Locations/timezones | Ask user during setup |
| Email provider | Gmail preferred; IMAP acceptable |
| Deployment target | Local Docker first; VPS later if desired |
| OpenAI usage | Only for cover notes (and Phase 4 CV bullets); strict budget gate |
| Codex subscription | Used by OpenClaw for source discovery and scoring tuning; flat-fee, no per-call budget |

## 17. References

| Topic | Reference |
|---|---|
| OpenClaw Docker | https://docs.openclaw.ai/install/docker |
| OpenClaw sandboxing | https://docs.openclaw.ai/sandboxing |
| OpenClaw scheduled tasks | https://docs.openclaw.ai/automation/cron-jobs |
| OpenClaw tools | https://docs.openclaw.ai/tools |
| OpenClaw usage tracking | https://docs.openclaw.ai/concepts/usage-tracking |
| OpenClaw token use and costs | https://docs.openclaw.ai/reference/token-use |
| OpenAI pricing | https://openai.com/api/pricing/ |
| We Work Remotely RSS | https://weworkremotely.com/remote-job-rss-feed |
| Remotive API/RSS | https://remotive.com/remote-jobs/api |
| Adzuna API | https://developer.adzuna.com/ |
| Arbeitnow API | https://www.arbeitnow.com/blog/job-board-api |
