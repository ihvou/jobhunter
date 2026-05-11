# OpenClaw Migration Plan

**Status**: Approved. Full migration. Codex executes this end-to-end.

**Audience**: Codex CLI agent doing the migration overnight, and any reviewer.

> Read this whole file before making changes. Phases can run in order; do not skip ahead.

## Why we are migrating

The current bot is a custom Python + Node.js implementation with a hand-rolled Telegram client, hand-rolled multi-turn LLM tool loop, hand-rolled prompt eviction, hand-rolled cron-replacement, etc. It works but accumulates maintenance debt that **OpenClaw solves natively**:

| Current custom code (~2,500 LOC) | Replaced by |
|---|---|
| `openclaw/worker/watcher.js` polling + multi-turn loop + eviction | OpenClaw gateway + agent runtime |
| `jobhunter/telegram.py` (Telegram client, callbacks, edit-or-send fallbacks) | OpenClaw Telegram channel |
| `openclaw/workspace/` file-based IPC | OpenClaw plugin SDK / skill tools |
| Manual cron-replacement (anomaly detection) | OpenClaw cron jobs |
| Manual IMAP polling (`collect_imap_alerts`) | OpenClaw Gmail Pub/Sub / `agenticmail` skill |
| Custom http_fetch tool | OpenClaw `firecrawl` / `exa` / `browser` plugins (much stronger) |
| Custom action approval keyboards | OpenClaw channel abstractions |
| Session memory (was filed as task #151) | OpenClaw native session model |

The Python domain logic — **scoring DSL, L1/L2 pipeline, source collectors, dedupe, DB schema, action handlers** — survives as a microservice consumed by the OpenClaw skill.

## Final architecture (target)

```
┌─────────────────────────────────────────────────────────────────────┐
│                       USER (Telegram, etc.)                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  OpenClaw Gateway (Docker)                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐   │
│  │  Channels  │  │   Agents   │  │   Skills   │  │ Sandboxing │   │
│  │ - Telegram │  │ - main     │  │ - jobhunter│  │ - Docker   │   │
│  │ (future:   │  │ - jobs     │  │ - leads    │  │ - non-main │   │
│  │  WA, etc.) │  │ - leads    │  │ - firecrawl│  │ - ro fs    │   │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐   │
│  │   Codex    │  │ Anthropic  │  │   Tools    │  │   Cron     │   │
│  │  (default  │  │  (fallback │  │  bridge to │  │   jobs     │   │
│  │   runtime) │  │  provider) │  │  jobhunter │  │            │   │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ skill tool calls (HTTP/stdio)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│       jobhunter-service (Python, Docker, sandboxed)                │
│  - SQLite DB (jobs, sources, scoring, agent_actions, etc.)         │
│  - L1 scoring DSL + interpreter                                    │
│  - L2 LLM relevance pass (OpenAI gpt-4o-mini)                      │
│  - Source collectors (RSS, JSON_API, ATS, IMAP)                    │
│  - Action handlers (sources_proposal, scoring_rule_proposal, etc.) │
│  - Profile + ICP file loaders                                      │
│  - Exposes: HTTP API on localhost (FastAPI or simple http.server)  │
└─────────────────────────────────────────────────────────────────────┘
```

## Phase 0 — Pre-flight (1 hour)

1. **Branch**: `git checkout -b openclaw-migration`. All work on this branch until parity is proven.
2. **Snapshot live DB**: `cp data/jobs.sqlite data/jobs.sqlite.pre-migration.bak`.
3. **Read these OpenClaw docs first** (`raw.githubusercontent.com/openclaw/openclaw/main/docs/`):
   - `docs/start/getting-started.md`
   - `docs/concepts/agent.md`, `docs/concepts/agent-runtimes.md`, `docs/concepts/agent-loop.md`
   - `docs/concepts/models.md`
   - `docs/tools/skills.md` (skill structure — SKILL.md format)
   - `docs/gateway/configuration.md`
   - `docs/gateway/sandboxing.md`
   - `docs/gateway/security/index.md`
   - `docs/channels/telegram.md`
   - `docs/automation/cron-jobs.md`
   - `docs/automation/gmail-pubsub.md`
4. **Discovery output** (Codex writes this to `MIGRATION_DISCOVERY.md` before coding):
   - Skill manifest format (one paragraph + sample)
   - Tool-registration model (one paragraph)
   - Telegram channel config keys we need (list)
   - Cron job declaration syntax (sample)
   - Sandbox config keys for our threat model (sample)
   - Codex CLI runtime integration (one paragraph: how our existing `codex login` carries over)

If any of these are unclear or contradictory, ask before proceeding.

## Phase 1 — OpenClaw install + minimal jobhunter skill (1-2 days)

### 1a. Install OpenClaw

- `npm install -g openclaw@latest`
- `openclaw onboard --install-daemon`
- Configure provider: OpenAI (use existing `~/.codex` OAuth tokens — should be detected automatically per CHANGELOG #79877).
- Configure Telegram channel with your existing bot token + chat ID.
- `openclaw doctor` should report green.

Document the resulting config file location (probably `~/.openclaw/config.json` or similar). Add the path to `.gitignore`.

### 1b. Create the jobhunter skill scaffold

Location: `skills/jobhunter/` in this repo (new directory).

Use **declarative SKILL.md format** (not the Plugin SDK — the SDK is churning, declarative skills are stable). Files:

```
skills/jobhunter/
├── SKILL.md              # manifest: name, description, prompt, tools
├── tools/                # tool definitions (declarative JSON or YAML)
│   ├── get_more_jobs.yml
│   ├── update_sources.yml
│   ├── tune_scoring.yml
│   ├── apply_action.yml
│   ├── show_history.yml
│   └── ...
├── prompts/              # agent prompts (split by intent)
│   ├── agent.md          # general /agent free-form
│   ├── discovery.md      # source discovery
│   └── tuning.md         # scoring tuning
└── README.md             # how to install/use the skill
```

The SKILL.md should describe the skill's purpose, supported channels (Telegram primary), and reference the tools. Per OpenClaw conventions: keep prompts terse, push logic into tool definitions.

### 1c. Stand up the Python service

Refactor the existing `jobhunter/` Python module into a localhost-only HTTP service:

```python
# jobhunter/service.py (new entrypoint)
# Wraps existing JobHunter class. Exposes:
#   POST /collect     → run collection, return {fetched, inserted}
#   POST /digest      → produce ranked job list as JSON
#   POST /agent/request   → ingest user_text, return session_id
#   POST /agent/poll      → return latest response for session_id
#   POST /action/apply    → apply a proposed action by id
#   POST /action/revert   → revert an applied action
#   GET  /history         → recent agent_actions
#   GET  /usage           → spend + quota
#   POST /irrelevant      → mark job irrelevant
#   POST /applied         → mark job applied
#   POST /snooze          → snooze job
#   POST /cover-note      → draft cover note
```

Service runs in its own Docker container, listens on `localhost:8765` only (no external port). Stdlib only (`http.server`) to keep the "no dependencies" rule. OR optional `fastapi` if simpler — get user approval if you add it.

Container in `docker-compose.yml`: `jobhunter-service`, restart unless-stopped, mount `./data` rw, `./config` ro, `./input` ro.

### 1d. Wire skill tools to call the service

Each tool in `skills/jobhunter/tools/` is a thin wrapper that:
1. Receives args from Codex/agent.
2. Calls the corresponding `jobhunter-service` HTTP endpoint.
3. Returns the response as a tool result.

OpenClaw's plugin tool system supports this via skill tool declarations. Example (verify exact format against docs):

```yaml
# skills/jobhunter/tools/get_more_jobs.yml
name: get_more_jobs
description: Fetch and rank the latest jobs across all enabled sources.
parameters: {}
implementation:
  type: http
  url: http://jobhunter-service:8765/digest
  method: POST
```

### 1e. Telegram routing

Configure OpenClaw's Telegram channel to route the user's bot. Set DM policy to `pairing` (default) — only your chat ID is approved.

Configure reply keyboard via OpenClaw's channel-specific reply markup if supported; else use plain text replies and let Codex compose the keyboard text. Verify against `docs/channels/telegram.md`.

### 1f. Phase 1 acceptance

- `./bin/openclaw start` brings up OpenClaw + jobhunter-service via compose.
- In Telegram: send "Get more jobs" → bot calls `get_more_jobs` tool → service returns digest → user sees it. Latency ≤ 5s for cached, ≤ 30s if collection needed.
- Send `/agent show me applied jobs` → Codex receives, calls `query_sql` (via OpenClaw's bridge) or `apply_action` to the service, returns a structured answer.
- `data/jobs.sqlite` is untouched (still readable, history preserved).

Stop and review with user before Phase 2.

## Phase 2 — Retire custom Telegram + worker code (2-3 days)

After Phase 1 parity is proven:

### 2a. Delete `openclaw/worker/`

- Remove the entire `openclaw/worker/` directory.
- Remove the `openclaw-gateway` service from `docker-compose.yml`.
- Remove worker-related env vars from `.env.example`.
- Update `Dockerfile` for jobhunter-service container.

### 2b. Retire `jobhunter/telegram.py`

- Delete the file.
- Replace all `self.telegram.X` calls in `jobhunter/app.py` with skill tool results (the skill is what talks to Telegram now).
- The Python service becomes "headless" — no Telegram knowledge.
- Tests for `telegram.py` go.

### 2c. Retire `openclaw/workspace/` IPC

- Delete the workspace directory.
- `agent.py`'s `AgentCoordinator.create_request` / `poll_done` retired.
- `coordinators.py`'s `DiscoveryCoordinator`/`ScoringCoordinator` `manual_handoff_message` / file-poll code retired.
- These flows become direct HTTP between skill tools and the service.
- The `agent_runs`, `discovery_runs`, `scoring_versions` DB tables stay (still useful for audit), just populated via service endpoints instead of via file polling.

### 2d. Replace custom multi-turn loop

Codex's tool calls now go through OpenClaw's agent runtime (Codex CLI default, per CHANGELOG #79877). The `runAgentCodex` JS function and all its eviction logic disappears.

### 2e. Update `bin/jobhunter` launcher

Replace with `bin/openclaw` that wraps `openclaw` CLI plus our jobhunter-service compose lifecycle:

```bash
./bin/openclaw start     # docker compose up jobhunter-service + openclaw onboard
./bin/openclaw stop      # docker compose down + openclaw gateway stop
./bin/openclaw logs      # tail both
./bin/openclaw status    # show health of both
./bin/openclaw shell     # exec into jobhunter-service
```

Keep the old `bin/jobhunter` as a deprecated wrapper for one release that points users at `bin/openclaw`.

### 2f. Phase 2 acceptance

- `docker ps` shows ONE container for jobhunter-service (no more openclaw-gateway, no more jobhunter Telegram container).
- OpenClaw gateway runs natively on host (or in its own container if user prefers).
- All 4 reply-keyboard buttons work via the skill.
- All per-job buttons (Applied/Irrelevant/Snooze/Cover note) work.
- `/agent`, `/history`, `/revert`, `/usage` all work.

## Phase 3 — Plug in OpenClaw's tool ecosystem (1 week)

Use OpenClaw's existing plugins to replace our custom collectors and improve quality.

### 3a. Replace custom http_fetch with `firecrawl` + `browser`

The jobhunter-service `http_fetch` was always weaker than `firecrawl` (renders JS, extracts structured data, handles SPAs). Update the skill's `agent.md` prompt to instruct Codex: *"For deep web research, prefer `firecrawl` or `browser` over plain http_fetch. firecrawl renders JS and returns structured page content."*

This directly addresses the user's complaint that the bot's source discovery is weaker than direct Claude desktop research. firecrawl + exa + browser are the missing tools.

### 3b. Replace IMAP polling with Gmail Pub/Sub + `agenticmail`

The current `collect_imap_alerts` polls every collection cycle. With OpenClaw's Gmail Pub/Sub, emails arrive as PUSH events:

- Configure Gmail Pub/Sub per `docs/automation/gmail-pubsub.md`.
- Each new email triggers a webhook → skill tool `process_email(msg_id, sender, subject, body)`.
- Service's `process_email` endpoint runs through the same template-matching pipeline (template lookup, `jobs_from_template` or generic fallback).
- The 30+ min polling latency drops to seconds.
- `imap_last_uid` field can be retired (replaced by per-message msg_id idempotency).

For email parsing itself: try the `agenticmail` skill from ClawHub if it covers HTML→jobs extraction natively. If not, keep our `email_parser_configs` DSL and just feed it from push events.

### 3c. Add `exa` for search-augmented discovery

In the discovery prompt, instruct Codex to use `exa` for niche source discovery queries: *"For Tier 2/3 search, prefer `exa` over generic web search — better recall on niche communities."*

### 3d. Add `agent-browser` or `firecrawl` for source validation

When a candidate source is proposed, validate it via `firecrawl` (renders JS, sees real content) rather than our SPA-detection heuristic.

### 3e. Phase 3 acceptance

- Source discovery via `/agent please update sources` returns 5+ candidates with at least 2 from Tier 2/3 niche sources.
- A new email arrives → webhook fires → job appears in DB within 10 seconds (vs current 30-min worst case).
- The 65 "needs template config" rows from the live DB drop to ≤5 after one week of operation (because per-sender templates get authored via skill).

## Phase 4 — Recurring + multi-agent for leadhunter (1 week)

### 4a. Cron jobs for recurring collection

Per `docs/automation/cron-jobs.md`, define:

```yaml
# In OpenClaw config:
cron:
  - name: jobs-collection
    schedule: "0 */4 * * *"   # every 4h
    skill: jobhunter
    tool: collect_all_sources

  - name: jobs-rescore-on-feedback-change
    schedule: "0 6 * * *"      # daily at 6am UTC
    skill: jobhunter
    tool: rescore_recent_jobs

  - name: jobs-discovery-monthly
    schedule: "0 0 1 * *"      # 1st of month
    skill: jobhunter
    tool: trigger_discovery
```

These replace the "background refresh if stale" logic and the anomaly-driven re-discovery (#137).

### 4b. Leadhunter skill (new)

`skills/leadhunter/` follows the same pattern as jobhunter:

- New SKILL.md
- New tools: `get_more_leads`, `add_lead_source`, `draft_pitch`, etc.
- New prompts: `lead_agent.md`, `lead_discovery.md`
- Same `jobhunter-service` Python backend, with new endpoints `/leads/digest`, `/leads/research`, etc.
- New `leads` table in SQLite (or generalize `jobs` to `opportunities` with `kind=job|lead` column — Codex chooses)
- New `input/icp.local.md` profile format
- Uses `apollo`, `linkdapi`, `abm-outbound`, `ai-lead-generator-skill` from ClawHub as composable building blocks where applicable.

### 4c. Multi-agent routing

Per `docs/gateway/configuration.md`, configure two agents:

```yaml
agents:
  jobs:
    skills: [jobhunter, firecrawl, exa, agenticmail]
    channels: [telegram-jobs]    # main bot, jobs context

  leads:
    skills: [leadhunter, apollo, linkdapi, agenticmail, firecrawl]
    channels: [telegram-leads]    # second bot, leads context

  defaults:
    sandbox:
      mode: non-main
      backend: docker
      workspaceAccess: rw
```

Each agent has its own session, history, prompt cache. They share the jobhunter-service container (or run separate services if data isolation needed — Codex decides).

### 4d. Phase 4 acceptance

- Cron jobs run on schedule; new jobs appear without user clicking.
- `/leads` reply keyboard button shows leads digest.
- `/agent find me 5 founders who raised Series A this week building AI products` produces a multi-turn research flow ending in lead candidates.
- Approving lead candidates lands them in the DB; `Draft pitch` produces a personalized DM you copy to LinkedIn manually.

## Security configuration (mandatory)

OpenClaw's default is FULL host access for main session. We override to match-or-exceed our current safety.

Add to OpenClaw config:

```yaml
agents:
  defaults:
    sandbox:
      mode: non-main              # main can stay full-host for trust; other sessions sandboxed
      backend: docker
      workspaceAccess: ro          # sandbox sees workspace read-only by default
    tools:
      profile: messaging
      deny:
        - group:automation
        - group:runtime
        - group:fs                 # deny broad-fs by default
      allow:
        - firecrawl                # explicit allow for web tools we need
        - exa
        - browser
        - http_fetch
        - jobhunter:*              # all jobhunter skill tools
        - leadhunter:*             # all leadhunter skill tools
      fs:
        workspaceOnly: true
      exec:
        security: deny             # no shell exec by default
        ask: always                # if Codex tries exec, prompt user
      elevated:
        enabled: false

channels:
  telegram:
    dmPolicy: pairing             # default — only paired chats accepted
    allowFrom:
      - "${TELEGRAM_ALLOWED_CHAT_ID}"
```

The Python `jobhunter-service` container itself:
- `read_only: true` rootfs
- `mem_limit: 512m`
- `cpus: 1.0`
- `cap_drop: [ALL]`
- `security_opt: ["no-new-privileges:true"]`
- `network_mode: bridge` (only reachable from openclaw-gateway container)
- No host port published

This is **stronger** than our current setup once configured. The Codex agent can do creative research via firecrawl/exa/browser but **cannot**:
- Write outside the workspace
- Exec arbitrary shell commands
- Modify our config or code
- Read `.env` or codex-home credentials
- Talk to non-allowed Telegram chats

## Surviving Python components

These do NOT change in functionality:

- `jobhunter/database.py` — schema, migrations, all queries
- `jobhunter/scoring.py` — L1 scoring DSL interpreter
- `jobhunter/llm.py` — L2 OpenAI client, budget gate
- `jobhunter/sources.py` — source collectors (RSS, JSON_API, ATS, IMAP)
- `jobhunter/agent_actions.py` — bounded action handlers
- `jobhunter/coordinators.py` — discovery/scoring shadow-test framework (still used by tools, just called via HTTP not file polling)
- `jobhunter/config.py` — settings loader (drop `JOBHUNTER_TASKS_PATH`, `JOBHUNTER_WORKSPACE_DIR` — no longer relevant)
- `jobhunter/budget.py` — OpenAI spend gate

These get new wrappers in `jobhunter/service.py` (the HTTP entrypoint), but their internals stay.

## Retired components

Delete:

- `openclaw/worker/` (entire directory)
- `openclaw/workspace/` (entire directory + `.gitignore` entry)
- `openclaw/codex-home/` (replaced by `~/.codex` reuse per CHANGELOG)
- `openclaw/prompts/` (moved to `skills/*/prompts/`)
- `jobhunter/telegram.py`
- `jobhunter/agent.py` (AgentCoordinator file-polling logic; HTTP endpoints replace it)
- `bin/jobhunter` (replaced by `bin/openclaw`)
- All `tests/test_openclaw_worker.py` tests
- Telegram-specific tests in `tests/test_app.py`
- Anything else that becomes unreachable after the above

## Data migration

- `data/jobs.sqlite` is **portable as-is**. The schema doesn't change.
- `data/email_samples/` survives — still useful for `email_parser_proposal` flows.
- `config/sources.local.json` + `config/scoring.local.json` + `input/profile.local.md` all survive untouched.
- `tasks.md` survives.
- `data/taskcandidates.md` (from #150) becomes an OpenClaw workspace file or stays where it is — service decides.

No data is lost. The Python service reads/writes the same files it does today.

## Contributing skills back to ClawHub

The user asked: yes, **we can contribute** `jobhunter` and `leadhunter` skills back to ClawHub later. ClawHub is the public marketplace; skills get listed there if approved by the maintainers. Plan to publish after 2+ weeks of stable operation with the migrated bot.

Branding considerations for upstream publication: rename the skills to be generic if needed (`jobsearch` / `leadprospecting` rather than `jobhunter` / `leadhunter`). User profile / ICP are inputs; the skill itself is generic.

## Testing strategy

- **Unit tests** for the Python service survive (mostly). Adapt test fixtures to call HTTP endpoints rather than calling JobHunter methods directly. Or: keep the in-process tests as-is and add a separate test suite for the HTTP layer.
- **Skill tests** are mostly manual the first time through. OpenClaw's Plugin SDK has test helpers (verify in docs) — use those for `skills/jobhunter/tools/*` validation.
- **End-to-end smoke test**: a small Python script that posts to the Telegram bot via Telegram's testing API, waits for response, asserts content. Gated by env (`JOBHUNTER_RUN_LIVE=1`).
- All existing tests must still pass at the end of each phase.

## Rollback

If any phase fails:
1. `git checkout main` to restore the pre-migration code.
2. `cp data/jobs.sqlite.pre-migration.bak data/jobs.sqlite` to restore the DB state.
3. `./bin/jobhunter start` (the old script) to bring up the old bot.
4. File an issue describing what failed.

Each phase commits independently — Phase 2 doesn't merge until Phase 1 is proven, etc.

## Acceptance criteria (overall)

The migration is considered complete when:

- [ ] OpenClaw gateway runs as the sole user-facing entrypoint.
- [ ] Telegram bot responds via OpenClaw's Telegram channel (no `jobhunter/telegram.py`).
- [ ] All 4 reply-keyboard actions + all 4 per-job actions work end-to-end.
- [ ] `/agent` free-form queries hit Codex via OpenClaw's agent runtime.
- [ ] Apply / Revert / History flow works.
- [ ] Cron job for periodic collection runs unattended.
- [ ] At least one email source has a working per-template parser authored via the agent flow.
- [ ] Sandbox config is in place (`non-main` mode, Docker backend, deny groups, fs workspaceOnly).
- [ ] All previous tests pass or have a documented replacement.
- [ ] User does a 24-hour stability check: no crashes, no missed digests, no silent failures.
- [ ] Old `openclaw/worker/`, `jobhunter/telegram.py`, file-based IPC are all deleted.
- [ ] `MIGRATION.md` is updated with the "Done at <commit>" footer.

## Codex execution notes

- Work in small commits. Each phase = at least one commit, ideally more.
- After each phase: run tests, run `docker compose config --quiet`, push to the branch.
- DO NOT push to `main` until user-approved.
- If a step is ambiguous or you discover something contradicting this plan, **stop and write the discrepancy to `MIGRATION_NOTES.md`** rather than guessing.
- Don't refactor more than necessary. Keep changes scoped to what's needed for migration.
- Respect existing safety constraints (no auto-apply, no LinkedIn login, no auto-PR to main).
