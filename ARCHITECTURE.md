# OpenClaw Job Search Agent Specification

## 1. Executive Summary

This specification defines a Docker-isolated OpenClaw job-search agent that finds, ranks, and helps act on high-quality job opportunities without using LinkedIn browser automation or logged-in job-board scraping.

The agent runs as a user-driven scout and analyst, not an autonomous applicant. It searches when the user asks, refines its sources and scoring rules with the user's approval, sends a ranked Telegram digest, and waits for explicit human feedback before drafting cover notes or marking actions as complete.

Primary interaction happens through a Telegram bot. Per-job decisions stay
inline; everything else lives on a persistent reply keyboard plus one
free-form agent surface:

| Per-job inline button | Meaning |
|---|---|
| `Irrelevant` | This job is a bad fit |
| `Remind me tomorrow` | Snooze for 24 hours and resend later, behind fresh jobs |
| `Give me cover note` | Generate a tailored cover note via the OpenAI API |
| `Applied` | User applied manually outside the bot |

| Reply-keyboard button | Meaning |
|---|---|
| `Get more jobs` | Run on-demand collection (§6.2) and send a fresh digest |
| `Update sources` | Ask OpenClaw + Codex to propose new validated sources (§6.5) |
| `Tune scoring` | Ask OpenClaw + Codex to propose ranking-rule changes (§8.4) |
| `Usage` | Show today's spend, agent quota, and recent activity |

| Free-form command | Meaning |
|---|---|
| `/agent <text>` | Ask OpenClaw + Codex to investigate, answer, and propose bounded actions (§6.6) |
| any normal text | Same agent surface; useful for feedback, read-only data questions, source/scoring requests, and profile edits |
| `/history`, `/revert <id>` | Audit and undo agent-applied changes (§6.8) |
| `/applied`, `/snoozed`, `/irrelevant` | Retrieve recent jobs by status |

The design favors safety, low account-ban risk, and low LLM cost. Per-job
deterministic scoring (Layer 1) is free. A bounded Layer 2 LLM relevance pass
(§8.5) judges only the top L1 survivors per click — small per-click cost. Free-
form requests, source discovery, and scoring tuning use the user's Codex
subscription via the OpenClaw worker (no per-call cost). Cover notes use the
metered OpenAI API. Every agent-proposed change goes through a Telegram
approval click and is recorded with a one-tap undo.

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
| Source abuse | Agent aggressively crawls career pages | No recursive crawl, per-host rate limits, per-source fetch caps, timeout/size caps, robots.txt opt-in |

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
| Narrow volumes only | Mount only `/jobhunter/data`, `/jobhunter/config`, and optional `/jobhunter/input` |
| Dedicated secrets | Use separate API keys/tokens only for this bot |
| Non-root runtime | Prefer non-root container user after setup |
| Network egress | Allow outbound HTTPS, but control behavior at application level |
| Logs | Persist logs to dedicated volume with rotation |

## 5. High-Level Architecture

The system runs as two cooperating Docker containers. **`jobhunter`** owns
deterministic work — collection, dedupe, L1 scoring, Telegram I/O, the
SQLite database, the OpenAI cover-note client, and the L2 relevance pass.
**`openclaw-gateway`** is an isolated Codex CLI worker that handles every
non-deterministic request (free-form `/agent`, source discovery, scoring
tuning) with bounded read-only tools and no shared database access.

The two containers communicate through a single shared workspace volume
using a file-based JSON contract — never HTTP, never shared SQLite writes.
Every write the agent proposes goes through a Telegram approval click and is
recorded in an audit table with a one-tap undo.

```text
                              +-------------------+
                              |  User on Telegram |
                              +---------+---------+
                                        |
                                        v
+============================= Docker Boundary =============================+
|                                                                            |
|  +---------------------------------+        +---------------------------+  |
|  |  jobhunter (Python, stdlib-only)   |        |  openclaw-gateway         |  |
|  |---------------------------------|        |  (Codex CLI worker)       |  |
|  | Telegram poll loop              |        |---------------------------|  |
|  | Reply keyboard + per-job inline |        | File-watch on workspace   |  |
|  | /agent + normal text            |        | Multi-turn Codex loop     |  |
|  | Action approval + audit/revert  |        | Read-only tools:          |  |
|  | Collectors (RSS/API/IMAP/HTML)  |        |   query_sql (SELECT only) |  |
|  | L1 deterministic scoring        |        |   read_file (allowlist)   |  |
|  | L2 LLM relevance pass (OpenAI)  |        |   list_dir (allowlist)    |  |
|  | Cover notes (OpenAI)            |        |   http_fetch (no priv IP) |  |
|  | Budget + rate-limit gates       |        | Per-request caps          |  |
|  | SQLite (private)                |        | Codex subscription auth   |  |
|  +---------------+-----------------+        +-------+-------------------+  |
|                  |                                  |                      |
|                  | request-<sid>.json (in)          |                      |
|                  | response-<sid>.json (out) <------+                      |
|                  v                                  ^                      |
|            +-----+----------------------------------+--+                   |
|            |  Shared workspace volume                  |                   |
|            |  ./openclaw/workspace/{discovery,         |                   |
|            |    tuning, agent}/  (rw on both sides)    |                   |
|            +-------------------------------------------+                   |
|                                                                            |
|  +------------------------+        +-----------------------------------+   |
|  | jobhunter private mounts: |        | OpenClaw private mounts:          |   |
|  |  /jobhunter/data (rw)     |        |  /jobhunter/data/jobs.sqlite (ro)*   |   |
|  |  /jobhunter/config (rw)   |        |  /openclaw/codex-home (rw, auth)  |   |
|  |  /jobhunter/input (ro)    |        |  /openclaw/prompts (ro)           |   |
|  +------------------------+        +-----------------------------------+   |
|                                    *for query_sql tool only                |
+============================================================================+
                  |                                  |
                  v                                  v
    Public RSS / JSON APIs / ATS boards     Codex (subscription)
    Email alerts via IMAP                   OpenAI API (cover notes, L2)
```

The diagram captures three patterns the rest of the spec relies on:

| Pattern | Where it shows up |
|---|---|
| **Bounded action set** | jobhunter accepts only the action `kind`s it has Python handlers for. Codex cannot ask jobhunter to execute arbitrary code. See §6.6, §10. |
| **Three LLM tiers** | L1 free, L2 OpenAI per-click ($), agent + cover note (Codex subscription / OpenAI). See §11. |
| **File-only cross-container channel** | Every agent flow is a JSON file in `workspace/<kind>/`. No network between the two containers. See §12.1. |


## 6. Core Scenarios

### 6.1 Scenario: Initial Setup

The primary input is a single profile file with two clearly separated
sections: a stable `# About me` (your free-text description) and a living
`# Directives` log (timestamped instructions you or the agent add over time).
A CV is optional and used only as secondary context for cover-note generation.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Writes `input/profile.local.md` with `# About me` section | Profile file |
| 2 | User | Optionally adds a text CV at `input/cv.local.md` for richer cover notes | CV file (optional) |
| 3 | User | Runs `./bin/jobhunter login` once to authorize the Codex subscription | Codex auth token in `openclaw/codex-home/` |
| 4 | User | Starts both containers with `./bin/jobhunter start` | jobhunter + openclaw worker running |
| 5 | Bot | Parses profile description, sends Telegram setup summary, shows persistent reply keyboard | User confirms scope |
| 6 | Bot | If a legacy `config/profile.local.json` is present, folds its lists into `# About me` and backs the JSON file up | Migrated profile |

Example profile description (free text is acceptable; structure is not required):

```text
# About me

Product manager. Product lead. Product owner. Head of product. Product builder.
Product engineer. Goal is to create product prototypes, MVPs, or implement new
features in existing products via Claude-Code/Codex. Another option for the
role goal is implementing AI-based features or optimizing business processes
via AI-based automation.

Key strengths: product/feature discovery (done in both outsourcing and product
company environments), getting insights from product analytics, managing
multi-stakeholder environments.

# Directives

[2026-05-02] Skip jobs whose description is primarily in German.
[2026-05-02] Marketing Manager / Product Marketing Manager titles are irrelevant.
[2026-05-02] Source priority: aggregators first; individual company pages only for AI-tooling vendors like Lovable.
```

The user can edit the file directly, or ask the agent in Telegram via
`/agent <text>` or normal free-form text. The agent always preserves the
section split and archives the previous version before any change (§6.7,
§6.8).

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Profile parsed | Target titles, role goals, positive keywords are extracted from `# About me` |
| Single source of truth | One file holds both the stable profile and the living directives; legacy `profile.local.json` is auto-migrated and backed up |
| CV optional | If `cv.local.md` present, used for cover notes; if absent, the bot still scores and digests jobs |
| Telegram connected | Bot can send and receive callback actions; persistent reply keyboard appears on first message |
| Safety policy active | No browser cookies, no auto-apply, no email-send permissions |

### 6.2 Scenario: On-Demand Job Collection

Job collection is triggered by the user, not by a fixed schedule. The Telegram
bot exposes a `Get more jobs` button. Clicking it runs a foreground collection
across all enabled sources, then sends a fresh digest of new (not-yet-shown)
jobs. A per-user rate limit prevents accidental hammering of sources. Active
sources are visited in priority order so high-priority sources fetch first. Each
source fetches a configured public/API/RSS/ATS endpoint or single static page;
the bot does not crawl or index sites recursively.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Taps `Get more jobs` (or types `/jobs`) | Trigger event |
| 2 | Bot | Checks rate limit (default: 1 collection / 10 minutes) | Allow or "please wait Ns" reply |
| 3 | Bot | Replies "Searching for new jobs..." | TG ack |
| 4 | Collectors | Fetch RSS/API/email/career pages from enabled sources, high-priority first | Raw job candidates |
| 5 | Normalizer | Converts source-specific fields | Canonical job records |
| 6 | Dedupe | Removes duplicates within and across sources | Unique jobs |
| 7 | L1 (rules) | Applies hard-reject rules + deterministic scoring from `config/scoring.json` (§8) | Scored jobs, candidate shortlist |
| 8 | L2 (LLM, optional) | Sends top N L1 survivors (default 30) to OpenAI for per-job verdict + priority + reason (§8.5). Skipped if `OPENAI_API_KEY` is unset; local fallback rejects only obvious bad role families | L2 verdicts persisted; jobs marked `not_relevant` are hidden |
| 9 | Sorter | Orders the digest by `priority desc, score desc, first_seen desc`; fresh jobs always above snoozed-due jobs | Sorted digest list |
| 10 | Telegram Sender | Sends digest of jobs not previously shown to the user; each card uses MarkdownV2 with the L2 reason as the "Why" line | New job cards |
| 11 | Bot | Marks each sent job in `digest_log` so it never re-appears unless the user explicitly snoozes it | Digest audit row |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| On-demand only | No cron-driven collection; first interaction is always a user click |
| Rate-limited | Repeated clicks within the rate window receive a short "wait" reply, not a fetch |
| Cross-source dedupe | Same company/title/canonical-URL not repeated across sources |
| No re-spam | A digest never contains a job already shown in a prior digest unless explicitly snoozed and now due |
| Hard filters respected | L1 hard-reject rules and L2 `not_relevant` verdicts both hide jobs from the digest |
| L2 bounded | Per-click L2 cost is bounded by `JOBHUNTER_L2_MAX_JOBS` (default 30); skipped jobs are not retried unless `/agent rescore_jobs` is invoked |
| Digest bounded | Max digest size is enforced; high-priority L2 verdicts surface above lower-priority higher-score jobs |
| Card readability | Each card uses MarkdownV2: bold title (with company prefix de-duplicated), L2 reason inline, ≤250-char description excerpt, source URL |
| Responsive | Button click is acknowledged in <2s; full digest delivered within ~30s for typical source counts |
| Chat hygiene | Tapping `Irrelevant`, `Snoozed`, or `Applied` deletes the original card from chat (failures past Telegram's 48h window are logged and ignored) |

Note: a daily safety-net background fetch is intentionally out of scope. If
the digest pool feels stale between user clicks, add it later as an opt-in
`JOBHUNTER_DAILY_REFRESH=1` flag.

### 6.3 Scenario: Telegram Feedback Loop

Each job card carries four inline buttons. Tapping any one of them logs the
feedback, transitions the job's status, and removes the card from chat (so
the chat log stays clean). The user can always retrieve recent jobs by status
via `/applied`, `/snoozed`, or `/irrelevant`.

| Button | User Intent | Immediate Action | Card behavior | Signal Used By |
|---|---|---|---|---|
| `Irrelevant` | Bad fit | Mark job rejected. Bot replies with a short prompt to send a one-line reason if there is a pattern to learn. | Card deleted | L2 directives, scoring tuning |
| `Remind me tomorrow` | Revisit later | Snooze 24 hours. When re-shown, the job sorts BELOW fresh jobs (§6.2 step 9) | Card deleted | None (neutral) |
| `Give me cover note` | Interested | Generate cover note via OpenAI API; budget-gated; promotes source from `test → active` | Card stays; cover-note message follows | Positive source signal |
| `Applied` | Application completed | Mark applied; promotes source from `test → active` | Card deleted | Strong positive source signal; included as training example for next scoring tuning |

The system never re-shows the same card. After tapping a button, the card is
gone from chat but every action is recorded. Use:

| Command | Returns |
|---|---|
| `/applied` | Last 10 jobs you marked Applied |
| `/snoozed` | Currently snoozed jobs and their wake-up time |
| `/irrelevant` | Last 10 jobs you marked Irrelevant |
| `/history` | Last 10 agent-applied actions (§6.8) |

For deeper analysis ("which sources had the highest applied rate?", "show me
patterns across my applied jobs"), ask in normal text or use `/agent
<question>` — see §6.10.

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

Source discovery is one specific shape of the broader agent flow described
in §6.6: the user taps `Update sources` (or types `/agent please run a
discovery cycle`); jobhunter writes a request to the shared workspace; OpenClaw
runs Codex against it with read-only validation tools (HTTP HEAD,
sample fetch, SPA detection); Codex returns one or more
`sources_proposal` actions; the user approves per-candidate before anything
lands in `config/sources.json`.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Taps `Update sources` (or types `/agent <text>` for free-form discovery instructions) | Trigger event |
| 2 | Bot | Writes `agent/request-<sid>.json` to shared workspace with profile, current sources, recent metrics, and the discovery intent | Agent request |
| 3 | Bot | Replies "Agent request queued · daily quota M/N" | TG ack |
| 4 | OpenClaw | Picks up new request file; runs Codex in a multi-turn loop with the worker's read-only tools (`http_fetch`, `read_file`, `query_sql`) | Validated candidate list |
| 5 | OpenClaw | Writes `agent/response-<sid>.json` with `proposed_actions: [{kind: "sources_proposal", payload: {operations: [...]}}]` and sets status to `done` | Response |
| 6 | Bot | Polls status; on `done`, sends Telegram message with answer + `[Apply N][Apply all][Reject all]` buttons | Approval prompt |
| 7 | User | Approves selected candidates | Selection |
| 8 | Bot | For each approved candidate: re-validates URL scheme + HEAD probe + dedupe; appends survivors to `config/sources.json` with `created_by='agent'`, `status='test'`; rejects unsafe ones with `skipped_invalid` | Updated sources + audit row |

Source storage and provenance:

| Provenance | Location | Lifecycle |
|---|---|---|
| Manually added | `config/sources.json`, `created_by='user'` | User edits directly; never modified by the agent |
| Agent-discovered | `config/sources.json`, `created_by='agent'`, `status='test'` initially | Appended only after user approval; auto-promotes to `active` on first `Applied` or `Give me cover note` from a job at that source |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| On-demand only | No cron-driven discovery |
| Validated candidates | Every approved candidate has been HEAD-probed by both OpenClaw (inside the worker) and jobhunter (at apply time) before being written to `sources.json` |
| User approval gate | Sources are not added to `sources.json` without an explicit Telegram approval click |
| Provenance preserved | Manual and agent-discovered sources are visually distinguishable via `created_by` |
| Subscription-only LLM cost | Discovery uses Codex via subscription; OpenAI per-call API is not invoked |
| Strategy guidance | The discovery prompt biases Codex toward aggregators / public ATS boards / RSS / IMAP filters; individual company pages only when the user's directives explicitly call for them |
| Lifecycle | New sources start in `status='test'`; promoted to `active` automatically on the first positive feedback signal |

### 6.6 Scenario: Free-Form Agent Request (`/agent`)

The unified entry point for everything that doesn't fit a fixed button.
Examples the user can type:

- *"please add a directive to skip Product Marketing Manager titles"*
- *"add this and figure out how to fetch it: https://jobs.dou.ua/vacancies/..."*
- *"you missed [URL] from 2 days ago, why? please adjust scraping"*
- *"please prioritize Product Builder roles that mention Claude or Codex"*
- *"send applied jobs to Codex and ask to optimize sources based on them"*

Each request flows through the same shape: jobhunter packages context →
OpenClaw runs Codex with bounded tools → Codex returns an answer plus zero
or more `proposed_actions` → user approves per-action.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Types `/agent <text>` or any normal free-form message in Telegram | Trigger event |
| 2 | Bot | Checks per-action cooldown (10s) and daily agent quota (default 20/day) | Allow or "wait/quota reached" reply |
| 3 | Bot | Writes `agent/request-<sid>.json` containing user text + full profile + sources summary + recent jobs sample + recent feedback summary | Agent request |
| 4 | Bot | Replies "Agent request queued · daily quota M/N" | TG ack |
| 5 | OpenClaw | Picks up the request file; runs Codex in a multi-turn loop (capped at 5 turns) with read-only tools — `query_sql`, `read_file`, `list_dir`, `http_fetch` — to investigate | Tool calls + tool results |
| 6 | OpenClaw | Wraps user-supplied text in untrusted-data sentinels; refuses any tool path Codex tries that touches `.env`, `codex-home`, host home, or non-SELECT SQL | Sandboxed Codex session |
| 7 | OpenClaw | Codex returns final JSON: `{user_intent_summary, answer, evidence_table?, proposed_actions[]}` | Response file |
| 8 | Bot | Validates schema, drops any `proposed_actions[].kind` not in the allowlist, sanitizes free-text fields | Cleaned response |
| 9 | Bot | Sends Telegram message with `answer` + per-action `[Apply N][Apply all][Reject all]` buttons (omitted entirely if there are no write actions) | Approval prompt |
| 10 | User | Approves chosen actions | Selection |
| 11 | Bot | For each approved action: dispatches to a Python handler that validates the payload, archives the previous file, applies the change, and inserts a row into `agent_actions` with the archive path | Applied changes + audit |

Allowed action kinds and what they do:

| `kind` | Effect |
|---|---|
| `directive_edit` | Append/replace lines in the `# Directives` section of `profile.local.md` (never touches `# About me`) |
| `profile_edit` | Replace the `# About me` section (never touches `# Directives`) |
| `sources_proposal` | Add / modify / disable rows in `config/sources.json` with HEAD-probe + scheme guard |
| `scoring_rule_proposal` | Replace `config/scoring.json` after schema validation + shadow test (§8.4) |
| `data_answer` | Read-only — content is shown to the user but no file is written |
| `human_followup` | Append a row to `tasks.md` for work that needs a human implementer |
| `email_parser_proposal` | Add an approved parser template for digest-style IMAP alert emails |
| `rescore_jobs` | Re-run L1 + L2 against jobs in a window |
| `bulk_update_jobs` | Status updates on a SELECT-defined set of jobs (terminal states only, hard-capped, CONFIRM required for >10 rows) |
| `backup_export` | Write a tar.gz of config + input + scoring archives to `data/backup/` |

Anything Codex returns outside this set is silently dropped and logged.

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Single primitive | One agent entry (`/agent`, plus normal free-form text) handles arbitrary requests; reply-keyboard buttons route through the same primitive under the hood |
| Approval-gated writes | No write action is applied without an explicit Telegram approval tap (or typed CONFIRM for bulk operations per §6.9) |
| Bounded action surface | Only allowlisted `kind`s are dispatched; unknown kinds are dropped + logged |
| No code execution | No `kind` ever maps to "run arbitrary code"; new capabilities require a new Python handler in jobhunter, not a new Codex output |
| Read-only tool surface | Codex can `SELECT`-only against the database, read allowlisted files, list allowlisted dirs, and `http_fetch` non-private URLs — nothing else |
| Cost capped per request | Per-request caps on Codex turns (5), SQL queries (20), file reads (10), HTTP fetches (5), and wall-clock seconds (180) |
| Cost capped per day | Daily caps on agent calls (20), source applies (5), scoring applies (3), bulk updates (2) |
| Audit trail | Every applied action lands in `agent_actions` with `archive_path`, `target_path`, and `result_message`; `/history` lists recent rows |

### 6.7 Scenario: Profile Management via Agent Chat

The user can read and edit `# About me` from chat without leaving Telegram.
There are no separate `/profile`, `/feedback`, or `/ask` command families:
profile work is handled by the same agent surface as everything else. The
`# Directives` section is preserved unless the user explicitly asks to edit
directives.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Types "show me my current profile" | Agent reads `input/profile.local.md` and returns a `data_answer` |
| 2 | User | Types "replace my about-me with ..." | Agent proposes a `profile_edit` action |
| 3 | User | Types "refine my about-me wording without changing intent" | Agent proposes a `profile_edit` action with cleaned-up text |
| 4 | User | Approves the proposed edit | Standard agent-action apply path (§6.6 step 11) — same archive + audit + revert chain |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Section split preserved | `profile_edit` touches `# About me` only; `# Directives` is byte-identical before and after |
| Approval-gated | Profile replacement requires the standard agent approval tap before the file changes |
| Refine is bounded | Refinement produces a `profile_edit` action with text the user reviews before applying |
| Reversible | Every `# About me` change is archived; `/revert <id>` restores it |

### 6.8 Scenario: Audit and Revert

Every action the agent applies is recorded in the `agent_actions` table.
File-mutating actions store the archive path of the previous version, so the
user can undo any change in one tap.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Types `/history` | Bot replies with last 10 agent-applied actions: `#<id> <kind> <status> · <one-line summary>` |
| 2 | User | Types `/revert <id>` | Bot looks up the action, opens its `archive_path`, writes it back to `target_path`, sets the original action's status to `reverted`, and inserts a new `agent_actions` row with `revert_target_id` pointing at the original |
| 3 | Bot | Telegram-replies with the new audit row id | User can re-revert (which is just a normal revert against the revert row) |

| Action `kind` | Reversible? |
|---|---|
| `directive_edit`, `profile_edit`, `sources_proposal`, `scoring_rule_proposal`, `backup_export` | Yes — `archive_path` of the previous file lets `/revert` restore byte-for-byte |
| `data_answer`, `human_followup` | No file change; `/revert` returns "no reversible archive" |
| `rescore_jobs`, `bulk_update_jobs` | DB mutations; not byte-reversible today (would need delta tracking — out of scope for Phase 3) |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Universal audit | Every applied action — including reverts — has a row in `agent_actions` |
| Byte-identical restore | Reverting a file-mutating action produces a file byte-identical to the pre-action state |
| Linked chain | `revert_target_id` connects each revert row to the action it undid; the chain is queryable from `/history` |
| Idempotent re-revert | Reverting a revert is allowed and works the same way |

### 6.9 Scenario: Bulk Operations with Confirmation

Some actions carry destructive blast radius — disabling many sources at
once, archiving many jobs, removing many scoring rules. These require a typed
text confirmation in addition to the Telegram approval tap.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Approves a `bulk_update_jobs` (>10 rows) or `sources_proposal` (>5 disables) or `scoring_rule_proposal` (>5 removed rules) action | Approval click |
| 2 | Bot | Action handler returns `requires_confirm=True` instead of applying | Pending-confirm state |
| 3 | Bot | Sends Telegram message: "<action summary>. Reply `CONFIRM <action_id>` within 60s to proceed." | Confirm prompt |
| 4 | User | Replies `CONFIRM <action_id>` within 60s | Confirm signal |
| 5 | Bot | Re-dispatches the action with `_confirmed=True`; handler skips the bulk threshold check | Action applied + audit row |

If the user does not reply (or replies with anything else) within 60s, the
pending-confirm state is dropped silently.

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Two-stage gate | Bulk operations cannot be applied with a single approval tap; the typed CONFIRM is required |
| Time-bounded | Pending confirms expire after 60s |
| Audited | Both the approval tap and the CONFIRM reply are recorded in the chat; the eventual `agent_actions` row notes the bulk count |

### 6.10 Scenario: Read-Only Data Query

For "what does my data say?" questions, the user asks in normal text or via
`/agent <question>`. Codex uses the worker's `query_sql`,
`read_file`, and `list_dir` tools to gather the data, then returns a
`data_answer` action with a prose answer plus an optional `evidence_table`
of rows.

Examples:

- *"jobs I applied to yesterday"* → SQL against `jobs` × `job_feedback` → answer + table of 2-3 rows
- *"which sources had the highest applied rate this month"* → aggregate SQL → answer + ranked source table
- *"why was \[URL\] not in today's digest?"* → SQL across `jobs` + `job_scores` + `digest_log` → answer with the firing rule and L2 verdict
- *"show me my current `# Directives`"* → file read → answer with the directive text
- *"diff scoring v2 and v3"* → two file reads → answer with the unified diff
- *"what rule fires most often?"* → aggregate SQL over `job_scores.fired_rules_json` → answer + table

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Types a data question | Trigger event |
| 2 | Bot | Same as `/agent` (§6.6) — writes the request file | Agent request |
| 3 | OpenClaw | Codex issues tool calls (`query_sql`, `read_file`, etc.) until it has enough data | Tool round trips |
| 4 | OpenClaw | Returns final JSON with `data_answer` action containing `answer` (prose) + `rows` (optional) + `evidence_table` (optional) | Response file |
| 5 | Bot | Renders `answer` + `evidence_table` (capped to first 10 rows) inline in Telegram; no approval buttons since no write actions are present | Read-only reply |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Read-only by construction | Data-answer responses contain only `data_answer` actions; no write actions are emitted |
| SQL safety | All `query_sql` calls are SELECT-only; PRAGMA / INSERT / UPDATE / DELETE / ATTACH are rejected |
| File safety | `read_file` allows `config/`, `input/`, `openclaw/workspace/`, `openclaw/prompts/`, `data/jobs.sqlite`; blocks `.env`, `codex-home/`, `/etc/`, `/home/`, `/root/` |
| Bounded results | Tables capped to 10 rows in chat; full results stay in the response file in `openclaw/workspace/agent/` |
| No approval needed | Read-only responses surface immediately; the main reply keyboard stays available, no inline buttons |

### 6.11 Scenario: Multi-Turn Source Discovery with Knowledge Base *(Phase 4)*

A future iteration of §6.5 where OpenClaw maintains cross-session memory.
Filed as task #105; not yet built.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Taps `Update sources` | Trigger event |
| 2 | OpenClaw | Reads `openclaw/knowledge_base/sources_tried.md` (cross-session memory of previously approved/rejected outcomes) into Codex's brief | Context primed |
| 3 | OpenClaw + Codex | Propose 5 candidates; for each, OpenClaw runs `http_fetch` + `looks_like_spa_shell` + sample parse | Validated/rejected per candidate |
| 4 | OpenClaw + Codex | For each failed candidate, re-prompt Codex with the failure reason; for SPAs, ask Codex to inspect for `__NEXT_DATA__` / `/api/` / `/_next/data/` paths and propose the underlying API URL | Refined list |
| 5 | OpenClaw | Writes the final response with `validated_candidates[]` + an `advisories[]` array describing what was tried for failed candidates | Response file |
| 6 | Bot | Standard `/agent` approval flow (§6.6) | User approves |
| 7 | OpenClaw | After approval, appends to `sources_tried.md` with the outcome | Knowledge base updated |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Cross-session memory | Previously rejected source patterns are not re-proposed in the next discovery cycle |
| SPA handling | Known-SPA URLs either come back with the underlying API URL or with a clear advisory ("checked X/Y/Z, no public API found") |
| Bounded retries | At most 2 re-prompt rounds per candidate to bound cost |

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

Robots.txt enforcement is opt-in through `JOBHUNTER_ROBOTS_TXT_RESPECT`.
The default is `ignore` because jobhunter is not a crawler: collection is
human-triggered, fetches one configured URL per source, and is bounded by a
per-host rate limiter, 30s timeout, 8MB response cap, SSRF protection, and
approval-gated source changes. Users who want stricter behavior can set
`trust` or `strict`.

## 8. Job Scoring

Per-job scoring is fully deterministic and free at runtime — no LLM call per
job. The scoring algorithm itself is generated and periodically refined by
OpenClaw + Codex, using the user's accumulated feedback as the training
signal. Algorithm updates are gated by user approval.

### 8.1 Two-Layer Architecture

| Layer | Runs | Cost per job | Updated by |
|---|---|---|---|
| Scoring rules (`config/scoring.json`) | Once per algorithm update | n/a | OpenClaw + Codex on demand (`Tune scoring` button) |
| Rule interpreter (`jobhunter/scoring.py`) | On every job | Free; deterministic | Code change (versioned in git) |

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

### 8.5 Layer 2 — LLM Relevance Pass

The deterministic ruleset (Layer 1) is fast and free but cannot read for
meaning. It can't tell apart "PM building with Claude/Codex" from "PM at a
fintech that mentions AI in its boilerplate." A bounded LLM pass on the top
L1 survivors closes that gap. L2 reads the user's full `# About me` plus
every line under `# Directives` — so free-form instructions like *"skip
non-English jobs"* or *"prioritize Product Builder roles that build with
Claude/Codex"* take effect immediately on the next `Get more jobs` click,
no rule update needed.

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | Bot | After L1, take the top N by score (default `JOBHUNTER_L2_MAX_JOBS=30`) | Candidate set |
| 2 | Bot | For each candidate, send a small prompt to OpenAI (`gpt-4o-mini` default) with: full `profile.local.md` + job title + company + first ~1500 chars of description | Per-job verdict |
| 3 | LLM | Return `{verdict: relevant\|borderline\|not_relevant, priority: high\|medium\|low, reason: <=200 chars, evidence_phrases: [...]}` | Structured judgment |
| 4 | Bot | Persist to `job_l2_verdicts` table; cache by job_id (one verdict per job, never re-asked) | Verdict cached |
| 5 | Bot | Filter the digest: drop `verdict=not_relevant`. Sort by `priority desc, score desc, first_seen desc` | Final digest order |
| 6 | Bot | Render the L2 `reason` as the "Why" line on each job card | Readable explanation |

If `OPENAI_API_KEY` is unset, a local fallback applies: it rejects only
obvious bad role families (Product Marketing, MLOps, DevOps, SRE) and
known unsupported-language requirements. Coarser than the API path but
keeps the system working without paid LLM access.

Cost profile (`gpt-4o-mini`, ~30 jobs per click):

| Item | Estimate |
|---|---|
| Input tokens per job | ~1,000 (profile + directives + job text) |
| Output tokens per job | ~80 (small structured response) |
| Cost per click | ~$0.002 - $0.005 |
| Cost per 100 clicks | <$1 |

The L2 layer also produces the **`priority`** signal that drives digest sort
order — see §6.2 step 9. High-priority jobs surface above lower-priority
higher-score jobs, so directives like "prioritize Product Builder roles"
reshape the digest immediately even when the L1 score is unchanged.

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Per-job cap | At most `JOBHUNTER_L2_MAX_JOBS` jobs per click reach OpenAI |
| Cached | A given job_id is sent to L2 at most once (re-runnable explicitly via `/agent rescore_jobs`) |
| Directive-driven | Adding a directive ("skip German jobs") is reflected on the next click without a rule update |
| Graceful fallback | When `OPENAI_API_KEY` is unset, the local fallback path runs and rejects only obvious bad role families |
| Sort priority | `priority=high` jobs appear above `priority=medium`/`low` jobs regardless of L1 score |
| Cost visibility | Daily and monthly OpenAI spend rolls up in the same `usage_log` as cover notes; visible via the `Usage` button |

### 8.6 Profile-Aware Tuning Inputs

When the user taps `Tune scoring`, the request payload to OpenClaw includes
not just aggregate counts but **concrete training examples** drawn from
recent feedback:

| Training signal | What's included |
|---|---|
| `applied[]` | Last 50 jobs the user marked Applied: title, company, description excerpt, fired rules, score, L2 verdict + reason |
| `irrelevant[]` | Last 50 jobs marked Irrelevant — same fields |
| `cover_note_requested[]` | Last 50 jobs the user requested a cover note for |
| `snoozed_then_applied[]` | High-signal jobs (snoozed, then later marked Applied) |

Codex sees the actual jobs the user reacted to, not just counts. The tuning
prompt instructs Codex to cite specific examples in each proposed rule's
`reason` field — so the user can read "added rule X because of these 3
Applied jobs."

### 8.7 Hard Filters

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

### 8.8 Output Explanation

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

### 8.9 Cost Profile

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
| `candidate_profile` | Cached normalized profile from `input/profile.local.md` (and optional `input/cv.local.md`) |
| `sources` | Source registry; provenance via `created_by`; lifecycle via `status` (`active`/`test`/`disabled`); `priority` (`high`/`medium`/`low`) drives collection order |
| `source_runs` | Fetch attempts, counts, errors, and cost |
| `jobs` | Canonical job records |
| `job_scores` | Per-job L1 score, hard-reject flag, fired rule IDs |
| `job_l2_verdicts` | Per-job L2 verdict (`relevant`/`borderline`/`not_relevant`), priority, reason, evidence phrases, model, tokens |
| `job_feedback` | Telegram button feedback (Applied / Irrelevant / Snooze / Cover-note) |
| `drafts` | Cover notes and CV suggestions |
| `usage_log` | OpenAI per-call token and cost records (cover notes + L2 relevance) |
| `digest_log` | Per-digest record: digest_id, timestamp, job_ids included; supports the "no re-spam" guarantee |
| `rate_limits` | Per-action throttle state (e.g. last `bot:collect` timestamp) |
| `discovery_runs` | Standalone source-discovery sessions: request, status, response file paths, candidate counts, approval outcome (legacy `Update sources` flow before §6.5 was rolled into the agent surface) |
| `scoring_versions` | History of `scoring.json`: version, generated_by, activated_at, shadow-test report |
| `agent_runs` | One row per `/agent` request: session_id, user_text, request/status/response paths, status, message |
| `agent_actions` | One row per applied (or rejected) agent action: id, session_id, kind, user_intent, summary, payload, archive_path, target_path, status, result_message, revert_target_id |

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

### 11.1 Three LLM Tiers

The system uses three clearly separated tiers. Each task is routed to
exactly one — never duplicated, never overlapped — based on cost shape and
where the work runs.

| Tier | Engine | Where | Cost shape | Used for |
|---|---|---|---|---|
| **L1 — Deterministic** | None | jobhunter (Python) | Free | Fetch, parse, dedupe, hard-reject, scoring rules, sort |
| **L2 — Per-job LLM** | OpenAI API (`gpt-4o-mini` default) | jobhunter | Pay-per-call (~$0.003/click for ~30 jobs) | L2 relevance pass on top L1 survivors (§8.5); cover notes; CV bullets (future) |
| **Agent — Subscription LLM** | Codex CLI (user's flat-fee subscription) | OpenClaw worker | No per-call cost; throttled by user clicks + per-day quota | Free-form `/agent` requests, source discovery, scoring tuning, profile refinement, ad-hoc data queries |

Per-task routing:

| Task | Tier | Frequency |
|---|---|---|
| Fetch and parse RSS/API/HTML | L1 | Per click |
| Dedupe (cross-source + within-source) | L1 | Per job |
| Hard filtering | L1 | Per job |
| L1 scoring (rule interpreter, §8) | L1 | Per job |
| L2 relevance pass (verdict + priority + reason) | L2 | Top N per click, cached |
| Cover note generation | L2 | On demand, per click |
| Source discovery (`Update sources`, `/agent`) | Agent | On demand |
| Scoring tuning (`Tune scoring`, `/agent`) | Agent | On demand |
| Free-form `/agent` requests and normal text | Agent | On demand |
| Profile refinement | Agent | On demand |
| `data_answer` queries (read-only SQL + file reads) | Agent | On demand |

### 11.2 Budget Rules

OpenAI-API calls (cover notes + L2 relevance pass) are budget-gated.
Subscription-based agent work has no per-call cost but is throttled to
prevent runaway prompts.

| Budget | Default | Applies To |
|---|---:|---|
| Daily OpenAI budget | `$0.50` | Cover notes + L2 relevance |
| Monthly OpenAI budget | `$10.00` | Cover notes + L2 relevance |
| Max cover-note drafts per day | `10` | Cover notes |
| L2 jobs per click | `30` | Cap on candidates sent to L2 |
| Max agent calls per day | `20` | Codex (anti-abuse) |
| Max source-applies per day | `5` | `sources_proposal` actions |
| Max scoring-applies per day | `3` | `scoring_rule_proposal` actions |
| Max bulk-update applies per day | `2` | `bulk_update_jobs` actions |
| Per-action cooldown | `10s` | Between `/agent` requests |
| Per-action rate limit | `1 / 10 min` | `Get more jobs` |

Per-agent-request guardrails enforced by the OpenClaw worker:

| Cap | Default |
|---|---:|
| `OPENCLAW_AGENT_MAX_CODEX_TURNS` | 5 |
| `OPENCLAW_AGENT_MAX_SQL_QUERIES` | 20 |
| `OPENCLAW_AGENT_MAX_FILE_READS` | 10 |
| `OPENCLAW_AGENT_MAX_HTTP_FETCHES` | 5 |
| `OPENCLAW_AGENT_MAX_WALL_SECONDS` | 180 |
| `OPENCLAW_MAX_PROMPT_CHARS` | 60,000 |

Budget gate behavior (cover notes):

| Condition | Behavior |
|---|---|
| Under budget | Allow OpenAI call |
| Daily budget exceeded | Telegram prompt: "Daily budget exceeded. [Override once] [Cancel]" |
| Monthly budget exceeded | Telegram prompt: "Monthly budget exceeded. [Override once] [Cancel]" |

Bulk action gate behavior (`bulk_update_jobs` >10 rows, `sources_proposal`
disabling >5 sources, `scoring_rule_proposal` removing >5 rules): the action
returns `requires_confirm`, the bot sends "Reply `CONFIRM <action_id>` within
60s," and only the typed confirmation re-dispatches the action with the
threshold check skipped (§6.9).

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

| Host Path | jobhunter container | OpenClaw container | Purpose |
|---|---|---|---|
| `./data` | `/jobhunter/data` rw | — (not mounted) | SQLite, logs, drafts |
| `./input` | `/jobhunter/input` ro | — (not mounted) | `profile.md`, optional `cv.md` |
| `./config` | `/jobhunter/config` rw | — (not mounted) | `sources.json`, `scoring.json`, `jobhunter.json` |
| `./openclaw/workspace` | `/jobhunter/workspace` rw | `/openclaw/workspace` rw | Discovery & tuning request/response files |
| `./openclaw/config` | — (not mounted) | `/openclaw/config` rw | OpenClaw's own state |

Note that **jobhunter writes its DB and config files only to its own private
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
| `JOBHUNTER_DAILY_BUDGET_USD` | yes | App-level hard budget |
| `JOBHUNTER_MONTHLY_BUDGET_USD` | yes | App-level hard budget |
| `GMAIL_CLIENT_ID` | optional | Gmail alert reader |
| `GMAIL_CLIENT_SECRET` | optional | Gmail alert reader |
| `EMAIL_IMAP_URL` | optional | IMAP alert reader |

### 12.3 Example Compose Skeleton

```yaml
services:
  openclaw-jobhunter:
    image: ghcr.io/openclaw/openclaw:latest
    container_name: openclaw-jobhunter
    restart: unless-stopped
    ports:
      - "127.0.0.1:18789:18789"
    environment:
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
      TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}"
      TELEGRAM_ALLOWED_CHAT_ID: "${TELEGRAM_ALLOWED_CHAT_ID}"
      JOBHUNTER_DAILY_BUDGET_USD: "0.50"
      JOBHUNTER_MONTHLY_BUDGET_USD: "10.00"
    volumes:
      - ./data:/jobhunter/data
      - ./input:/jobhunter/input:ro
      - ./config:/jobhunter/config
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
| `Update sources` flow: jobhunter ↔ OpenClaw ↔ Codex via shared workspace | Yes |
| Per-candidate validation by OpenClaw (HTTP HEAD, sample fetch, parseability) | Yes |
| Telegram approval gate; agent-discovered sources written to `sources.json` with `created_by='agent'` | Yes |
| `discovery_runs` table for audit | Yes |

### Phase 3: Agent-Driven Scoring Tuning

| Feature | Included |
|---|---|
| `Tune scoring` flow: jobhunter ↔ OpenClaw ↔ Codex via shared workspace | Yes |
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
| Daily safety-net background collection (`JOBHUNTER_DAILY_REFRESH=1`) | Opt-in only |

## 15. Acceptance Criteria

| Area | Criteria |
|---|---|
| Safety | No LinkedIn cookies, no auto-apply, no outbound recruiter messaging |
| Docker | Both containers run in Docker; jobhunter ↔ OpenClaw share only `./openclaw/workspace/`; OpenClaw also gets `data/jobs.sqlite:ro` for the `query_sql` tool |
| Telegram per-job | Digest and four required per-job buttons work; cards delete from chat after Irrelevant/Snooze/Applied |
| Telegram reply keyboard | `Get more jobs`, `Update sources`, `Tune scoring`, `Usage` buttons work; persistent across sessions |
| Free-form commands | `/agent`, normal text, `/history`, `/revert`, `/applied`, `/snoozed`, `/irrelevant` all parse and route correctly |
| On-demand collection | No cron; rate-limited; cross-source dedupe; never re-shows previously digested jobs; high-priority sources fetch first |
| L1 scoring | Fully deterministic; zero LLM calls per job; word-boundary matching only; rules live in `config/scoring.json` |
| L2 relevance | Capped at `JOBHUNTER_L2_MAX_JOBS` per click; cached per job; reads full `# About me` + `# Directives`; gracefully falls back to local heuristic when no `OPENAI_API_KEY` |
| Single profile file | `input/profile.local.md` with `# About me` + `# Directives` is the sole source of truth; legacy `profile.local.json` is auto-migrated and backed up |
| Source discovery | Triggered on demand via `Update sources` or `/agent`; OpenClaw + Codex iterate with read-only validation tools; user approves per-candidate; written to `sources.json` with `created_by='agent'` |
| Scoring tuning | Triggered on demand; OpenClaw + Codex propose rules in the §8.3 DSL; shadow-tested; user approves before activation; previous version archived; ruleset schema validated before swap |
| Agent surface — bounded | Only allowlisted action `kind`s are dispatched (`directive_edit`, `profile_edit`, `sources_proposal`, `scoring_rule_proposal`, `email_parser_proposal`, `data_answer`, `human_followup`, `rescore_jobs`, `bulk_update_jobs`, `backup_export`); unknown kinds dropped + logged; no kind maps to "execute arbitrary code" |
| Agent surface — approval-gated | Every write action requires a Telegram approval tap; bulk operations require an additional typed `CONFIRM <id>` reply within 60s |
| Agent surface — audit + revert | Every applied action lands in `agent_actions` with `archive_path` + `target_path`; `/history` lists recent actions; `/revert <id>` restores file-mutating actions byte-for-byte |
| Agent surface — read-only tools | Codex's worker tools are SELECT-only SQL, allowlist-only file reads, allowlist-only directory listings, and HTTP fetch with private-IP rejection |
| Agent surface — capped | Per-request caps on Codex turns, SQL queries, file reads, HTTP fetches, prompt size, wall-clock seconds; per-day caps on agent calls and per-action-kind applies |
| Cost | OpenAI API used for cover notes + L2 relevance only; daily/monthly OpenAI budget gate blocks excess; Codex use is subscription-bounded |
| Audit | Every job, L1 score, L2 verdict, feedback action, OpenAI call, agent run, applied agent action, discovery run, and scoring version is logged |

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
