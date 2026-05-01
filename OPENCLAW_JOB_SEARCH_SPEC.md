# OpenClaw Job Search Agent Specification

## 1. Executive Summary

This specification defines a Docker-isolated OpenClaw job-search agent that finds, ranks, and helps act on high-quality job opportunities without using LinkedIn browser automation or logged-in job-board scraping.

The agent runs as an autonomous scout and analyst, not an autonomous applicant. It continuously improves where and how it searches, sends a ranked Telegram digest, and waits for explicit human feedback before drafting cover notes or marking actions as complete.

Primary interaction happens through a Telegram bot with inline feedback buttons:

| Button | Meaning | System Action |
|---|---|---|
| `Irrelevant` | This job is a bad fit | Down-rank similar jobs, log rejection reason if supplied |
| `Remind me tomorrow` | Interesting, but not now | Snooze job for 24 hours and resend |
| `Give me cover note` | Prepare an application note | Generate tailored cover note and optional CV bullet suggestions |
| `Applied` | User applied manually | Mark job as applied, update company/source success metrics |

The design favors safety, low account-ban risk, and low LLM cost.

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

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | User | Places CV and preferences into mounted input folder | `cv.pdf`, `preferences.yaml` |
| 2 | User | Starts Dockerized OpenClaw Gateway | Running gateway |
| 3 | Agent | Parses CV and preferences | Normalized profile |
| 4 | Agent | Builds initial source list | Seeded sources |
| 5 | Agent | Sends Telegram setup summary | User confirms scope |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| CV parsed | Skills, seniority, roles, regions, exclusions extracted |
| Preferences loaded | Salary, timezone, location, role, company-stage preferences available |
| Telegram connected | Bot can send and receive callback actions |
| Safety policy active | No browser cookies, no auto-apply, no email-send permissions |

### 6.2 Scenario: Scheduled Job Collection

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | Cron | Starts collection every 2-6 hours | Collection job |
| 2 | Collectors | Fetch RSS/API/email/career pages | Raw job candidates |
| 3 | Normalizer | Converts source-specific fields | Canonical job records |
| 4 | Dedupe | Removes duplicates | Unique jobs |
| 5 | Rule Filter | Applies hard constraints | Candidate shortlist |
| 6 | Ranker | Scores jobs | Ranked jobs |
| 7 | Telegram Sender | Sends digest | Inline action buttons |

Acceptance criteria:

| Criterion | Pass Condition |
|---|---|
| Dedupe works | Same company/title/link not repeated |
| Hard filters respected | Excluded locations/sectors/levels are removed |
| Digest bounded | Max digest size is enforced |
| Cost bounded | LLM call budget checked before analysis |

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

### 6.5 Scenario: Dynamic Source Discovery

| Step | Actor | Action | Output |
|---:|---|---|---|
| 1 | Cron | Runs weekly source-discovery job | Discovery session |
| 2 | Agent | Reviews profile, recent matches, feedback | Source strategy context |
| 3 | Agent | Searches public web for platforms/companies/query patterns | Candidate sources |
| 4 | Agent | Scores candidate sources | Source review queue |
| 5 | Agent | Sends Telegram summary | User approves source classes or specific sources |
| 6 | Agent | Adds approved sources to test pool | New source experiments |

Source-discovery prompt template:

```text
Find high-signal job sources for this candidate profile.
Prioritize low-competition, fresh, direct-application, remote-compatible sources.
Avoid LinkedIn logged-in scraping or any source requiring cookie automation.
Return candidate sources as structured JSON:
- source_name
- source_type
- access_method
- expected_signal
- risk_level
- polling_frequency
- first_query_or_url
- why_it_matches
```

### 6.6 Scenario: Dynamic Search Adjustment

| Signal | Interpretation | Adjustment |
|---|---|---|
| High `Irrelevant` rate for source | Source has poor fit | Lower source priority |
| Many duplicate jobs | Source overlaps with better feeds | Reduce polling frequency |
| Many jobs fail salary/location filter | Query too broad | Add stricter query terms |
| User requests cover notes often | Source has strong relevance | Increase priority |
| User marks applied | Source produces actionable roles | Increase trust score |
| Good companies, wrong roles | Query taxonomy mismatch | Adjust title/keyword mapping |
| Too few results | Search too narrow | Add adjacent titles and technologies |

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

### 7.3 Source Score

Each source receives a score from 0 to 100.

| Component | Weight | Description |
|---|---:|---|
| Relevance | 30 | Average job match score |
| Freshness | 15 | Time between posting and discovery |
| Uniqueness | 15 | Non-duplicate rate |
| Actionability | 15 | User clicks `Give me cover note` or `Applied` |
| Safety | 10 | No logged-in automation or suspicious scraping |
| Data quality | 10 | Salary, location, requirements available |
| Cost efficiency | 5 | Useful jobs per fetch/LLM cost |

Source status:

| Score | Status | Behavior |
|---:|---|---|
| 80-100 | Core | Poll frequently |
| 60-79 | Active | Poll normally |
| 40-59 | Test | Poll lightly |
| 20-39 | Deprioritized | Poll rarely |
| 0-19 | Disabled | Stop unless user re-enables |

## 8. Job Scoring

### 8.1 Job Match Score

| Component | Weight | Examples |
|---|---:|---|
| Role/title fit | 20 | Senior engineer, founding engineer, AI product engineer |
| Skill fit | 20 | Python, TypeScript, LLMs, infra, product engineering |
| Seniority fit | 10 | Senior/staff/founding vs junior/manager-only |
| Remote/location fit | 15 | Remote worldwide, Europe/Asia overlap |
| Company fit | 10 | Stage, domain, culture, funding |
| Compensation fit | 10 | Salary/equity transparency and floor |
| Application friction | 5 | Direct form, email, simple application |
| Freshness | 5 | Recently posted |
| User feedback similarity | 5 | Similar to jobs user liked/applied |

### 8.2 Hard Filters

| Filter | Example |
|---|---|
| Excluded domains | Crypto, gambling, defense, adult, etc. |
| Location mismatch | US-only when user cannot work US-only |
| Seniority mismatch | Intern/junior roles |
| Compensation mismatch | Below salary floor if explicit |
| Role mismatch | Sales/support/non-technical if not desired |
| Duplicate | Same job already seen/applied/rejected |

### 8.3 Output Explanation

Each Telegram job card should include:

| Field | Description |
|---|---|
| Title | Job title |
| Company | Company name |
| Score | 0-100 |
| Location | Remote/region constraints |
| Source | Where it was found |
| Why it matches | 2-4 concise bullets |
| Concerns | 1-3 possible issues |
| Link | Source URL |
| Buttons | Inline feedback buttons |

## 9. Telegram Bot Design

### 9.1 Digest Message Format

```text
Top matches from the last 6 hours

1. Senior AI Product Engineer - ExampleCo
Score: 91
Source: Ashby career page
Location: Remote, Europe overlap

Why it matches:
- LLM product engineering
- TypeScript + Python stack
- Senior IC role with startup ownership

Concern:
- Salary not listed

[Irrelevant] [Remind me tomorrow] [Give me cover note] [Applied]
```

### 9.2 Callback Payloads

| Button | Callback Data | Required Fields |
|---|---|---|
| `Irrelevant` | `job:irrelevant:<job_id>` | `job_id`, `user_id`, timestamp |
| `Remind me tomorrow` | `job:snooze_1d:<job_id>` | `job_id`, snooze_until |
| `Give me cover note` | `job:cover_note:<job_id>` | `job_id`, draft_request_id |
| `Applied` | `job:applied:<job_id>` | `job_id`, applied_at |

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
| `candidate_profile` | Normalized profile from CV and preferences |
| `sources` | Source registry and source scoring |
| `source_runs` | Fetch attempts, counts, errors, and cost |
| `jobs` | Canonical job records |
| `job_scores` | Scoring breakdowns |
| `job_feedback` | Telegram button feedback |
| `drafts` | Cover notes and CV suggestions |
| `usage_log` | Token and estimated cost records |
| `budget_state` | Daily/monthly cost counters |
| `experiments` | Source/query experiments |

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

| Task | Preferred Method | Model |
|---|---|---|
| Fetch and parse RSS/API | Deterministic code | None |
| Dedupe | Hashing and rules | None |
| Hard filtering | Rules | None |
| Embedding similarity | Local embedding model | Local |
| Basic job scoring | Rules + local embeddings | None/local |
| Short match explanation | Small LLM | GPT-5.4 nano or mini |
| Source discovery | Small/medium LLM | GPT-5.4 mini |
| Cover note | Small/medium LLM | GPT-5.4 mini |
| Deep company strategy | Manual approval | GPT-5.4 mini or larger if approved |

### 11.2 Budget Rules

| Budget | Default |
|---|---:|
| Daily LLM budget | `$0.50` |
| Monthly LLM budget | `$10.00` |
| Max LLM-analyzed jobs per run | `30` |
| Max LLM-analyzed jobs per day | `150` |
| Max source-discovery runs | `1/week` |
| Max cover-note drafts per day | `10` |

Budget gate behavior:

| Condition | Behavior |
|---|---|
| Under budget | Allow LLM call |
| Near daily budget | Use local/rule-only scoring |
| Daily budget exceeded | Stop LLM calls until next day |
| Monthly budget exceeded | Stop LLM calls until reset or user override |
| User requests cover note after budget exceeded | Ask for explicit override in Telegram |

### 11.3 Cost Visibility

| Surface | Content |
|---|---|
| Telegram daily digest | `Spent today`, `spent this month`, `jobs processed` |
| Weekly report | Source quality, costs, applied count, rejected count |
| OpenClaw CLI | `openclaw gateway usage-cost --days 7 --json` |
| OpenClaw chat | `/usage full`, `/usage cost`, `/status` |
| SQLite | Per-call usage log |

## 12. OpenClaw Docker Deployment

### 12.1 Volumes

| Host Path | Container Path | Access | Purpose |
|---|---|---|---|
| `./data` | `/jobbot/data` | read/write | SQLite, logs, drafts |
| `./input` | `/jobbot/input` | read-only | CV and preferences |
| `./config` | `/jobbot/config` | read/write | OpenClaw config |

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

## 14. Dynamic Optimization Reports

### 14.1 Daily Telegram Summary

| Field | Description |
|---|---|
| Jobs collected | Raw count |
| Jobs after dedupe | Unique count |
| Jobs after filters | Shortlist count |
| Top source | Highest scoring source today |
| Spend today | LLM spend estimate |
| Actions | Rejected, snoozed, cover notes, applied |

### 14.2 Weekly Strategy Report

| Section | Questions Answered |
|---|---|
| Source winners | Which sources produced the most useful jobs? |
| Source losers | Which sources produced irrelevant or duplicate jobs? |
| Query changes | What search terms should change? |
| Company targets | Which companies should be monitored directly? |
| New experiments | Which source experiments should be tested next week? |
| Budget | Did the agent stay within cost limits? |

### 14.3 Example Weekly Recommendation

```text
Recommendation:
Increase polling for Ashby AI/devtools career pages from daily to every 6 hours.

Evidence:
- 14 unique jobs found
- 7 scored above 80
- 3 cover-note requests
- 1 applied
- Low duplicate rate

Cost:
- $0.08 LLM analysis this week

Risk:
- Low. Public career pages only.
```

## 15. Implementation Phases

### Phase 1: Safe MVP

| Feature | Included |
|---|---|
| Dockerized OpenClaw Gateway | Yes |
| Telegram digest | Yes |
| CV/preferences parsing | Yes |
| RSS/API collectors | Yes |
| SQLite dedupe/logging | Yes |
| Rule-based filtering | Yes |
| Basic LLM explanation | Yes |
| Feedback buttons | Yes |
| Auto-apply | No |
| Browser automation | No |

### Phase 2: Dynamic Source Optimization

| Feature | Included |
|---|---|
| Source score model | Yes |
| Weekly source discovery | Yes |
| Query experiment tracking | Yes |
| Company target list generation | Yes |
| Weekly strategy report | Yes |
| Budget-aware LLM routing | Yes |

### Phase 3: Application Assistance

| Feature | Included |
|---|---|
| Cover note generation | Yes |
| CV bullet suggestions | Yes |
| Company research summary | Yes |
| Application checklist | Yes |
| Manual apply tracking | Yes |
| Auto-submit | No |

### Phase 4: Optional Advanced Features

| Feature | Notes |
|---|---|
| Browser preview for public pages | Only non-logged-in pages |
| Dedicated web dashboard | Optional; Telegram can be enough |
| Multi-CV variants | Useful for different role clusters |
| Interview follow-up tracking | Later workflow |
| Recruiter outreach drafts | Draft only; user sends manually |

## 16. Acceptance Criteria

| Area | Criteria |
|---|---|
| Safety | No LinkedIn cookies, no auto-apply, no outbound recruiter messaging |
| Docker | Gateway runs in Docker with narrow volumes |
| Telegram | Digest and four required buttons work |
| Feedback | Button clicks update job status and learning metrics |
| Source discovery | Agent proposes new sources with risk and expected signal |
| Dynamic adjustment | Source priority changes based on metrics |
| Cost | Daily/monthly budget gate blocks excess LLM calls |
| Audit | Every job, score, feedback action, and LLM call is logged |

## 17. Open Questions

| Question | Default Assumption |
|---|---|
| Preferred roles | Extract from CV, then ask user to confirm |
| Salary floor | Ask user during setup |
| Locations/timezones | Ask user during setup |
| Email provider | Gmail preferred; IMAP acceptable |
| Deployment target | Local Docker first; VPS later if desired |
| LLM provider | OpenAI API key for predictable cost |
| Use Codex quota | Not for autonomous routine analysis; reserve for development/manual tasks |

## 18. References

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

