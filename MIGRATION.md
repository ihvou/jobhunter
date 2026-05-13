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

Stop and review with user before Phase 1.5.

## Phase 1.5 — Dockerize the OpenClaw gateway (mandatory before Phase 2) (0.5-1 day)

**Status**: Done at `9605d2e` for the implementation commit. User-run parity verification is still required before Phase 2 destructive deletions.

**Why this exists**: Phase 1 (commit `aa71239`) installed OpenClaw on the host (`npm install -g openclaw@latest` + `openclaw onboard --install-daemon`). The original `ARCHITECTURE.md` listed *"Run the full OpenClaw Gateway inside Docker with narrow mounted volumes and scoped secrets"* as **P0**. Host install was a Phase 1 shortcut that we are now correcting before any destructive deletion in Phase 2 lands.

**Why Docker-in-Docker for the gateway is the right call** for this user's threat model: the gateway process itself is the most-attackable surface (channel adapters, plugins, agent runtime). On host it can read `~/.codex`, `~/.ssh`, `~/.aws`, all user files. In a container with narrow mounts, it sees only what we explicitly grant. The per-session Docker sandbox (which would require mounting `/var/run/docker.sock` into the gateway — the spec's hard line) is **off** for this configuration; it does not block any tool we actually use (firecrawl, exa, browser-via-image, agenticmail, apollo, MCP-bridged jobhunter tools, Codex CLI multi-turn, all channels work identically with sandbox off). The marginal value of nested sandboxing is small when the gateway itself is already containerized with limited mounts.

OpenClaw officially supports Docker install per `docs/install/docker.md`. Pre-built image: `ghcr.io/openclaw/openclaw:latest`. Setup script `./scripts/docker/setup.sh` exists upstream but **we drive Compose ourselves** to keep our hardening (read-only rootfs, capability drops, no-new-privileges) consistent with `jobhunter-service`.

### 1.5a. Add `openclaw-gateway` service to `docker-compose.yml`

Concrete spec:

```yaml
  openclaw-gateway:
    image: ghcr.io/openclaw/openclaw:latest    # pin to a specific tag, not :latest
    container_name: openclaw-gateway
    restart: unless-stopped
    depends_on:
      jobhunter-service:
        condition: service_healthy
    environment:
      OPENCLAW_DISABLE_BONJOUR: "1"
      JOBHUNTER_SERVICE_URL: "http://jobhunter-service:8765"
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:-}
      TELEGRAM_ALLOWED_CHAT_ID: ${TELEGRAM_ALLOWED_CHAT_ID:-}
    volumes:
      - openclaw_home:/home/node             # persistent state (config, prompt cache, etc.)
      - ./skills:/openclaw/skills:ro          # our skill manifests
      - ./config/openclaw.json:/openclaw/config.json:ro  # gateway config
      - ~/.codex:/home/node/.codex:ro         # Codex CLI auth carried over
    ports:
      - "127.0.0.1:18789:18789"               # Control UI / channel callbacks, loopback only
    read_only: true
    tmpfs:
      - /tmp
    mem_limit: 1g
    cpus: 1.5
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  openclaw_home:
    driver: local
```

**Pin the image tag** to a specific date-based release (e.g., `ghcr.io/openclaw/openclaw:2026.5.10`) rather than `:latest`. The Dependabot/Renovate workflow can bump it later. Document the chosen tag in `MIGRATION_NOTES.md`.

The `openclaw-gateway` and `jobhunter-service` containers share Docker's default bridge network, so `http://jobhunter-service:8765` resolves between them. The MCP bridge changes from stdio over a host-side spawn to **HTTP-over-bridge-network** if OpenClaw can launch our Python MCP server inside its own container — investigate during this phase. If stdio is preferred, copy `jobhunter/openclaw_mcp.py` into the gateway image at build time via an overlay layer, OR mount the repo at `/opt/jobhunter:ro` and let the gateway spawn `python3 -m jobhunter.openclaw_mcp` against it. Document the chosen approach in `MIGRATION_NOTES.md`.

### 1.5b. Drop the `openclaw onboard --install-daemon` step

Replace with the Docker-equivalent onboarding flow. Per upstream `docs/install/docker.md`:

```bash
docker compose run --rm --no-deps --entrypoint node openclaw-gateway \
  dist/index.js onboard --mode local --no-install-daemon
```

The setup writes config to the `openclaw_home` volume (mounted at `/home/node`). Codex captures the final command sequence in `bin/openclaw` (`./bin/openclaw onboard` invokes it).

### 1.5c. Update `bin/openclaw`

- `start` now `docker compose up -d jobhunter-service openclaw-gateway` (both).
- `stop` brings both down.
- `restart` recreates both.
- `logs` defaults to following both services; `logs gateway` and `logs service` for targeted.
- `status` reports both containers' state.
- `shell gateway` and `shell service` (default service).
- `onboard` invokes the Docker-equivalent onboarding (1.5b).
- `config` still prints the MCP server snippet but adjusts paths to be container-relative (e.g., `command: "python3"`, `args: ["-m", "jobhunter.openclaw_mcp"]`, `cwd: "/opt/jobhunter"` if we mounted that, or via HTTP MCP if we go that route).

### 1.5d. Update `config/openclaw.example.json5`

- Set `agents.defaults.sandbox.mode: "off"` (was `"non-main"` — pointless when gateway is itself containerized).
- Keep all `tools.deny` lists (group:automation, group:runtime, group:fs) — these are independent of sandbox and still apply.
- Keep `tools.fs.workspaceOnly: true`, `tools.exec.security: "deny"`, `tools.exec.ask: "always"`.
- Add comment explaining the choice and pointing at this section of MIGRATION.md.
- Telegram allowlist unchanged.

### 1.5e. Update `MIGRATION_NOTES.md`

Add a section "Phase 1.5: Dockerize gateway" with:
- The image tag pinned and rationale
- The chosen MCP transport (stdio with mounted repo vs HTTP-over-bridge)
- The Codex auth migration approach (read-only mount of `~/.codex`)
- The sandbox=off decision and which tools are protected by what (deny-lists, mount discipline)
- Any surprises Codex finds during the Docker onboarding flow

### 1.5f. Update `MIGRATION.md` itself

After Phase 1.5 lands, update this section's status to "Done at <commit>".

### Phase 1.5 acceptance

- [ ] `docker compose ps` shows TWO running containers: `jobhunter-service` and `openclaw-gateway`. Both healthy.
- [ ] `docker exec openclaw-gateway env` shows it cannot see host home dir secrets (only what we mounted).
- [ ] `openclaw_home` volume contains the gateway's persistent state (config, prompt cache, channel pairings).
- [ ] Codex CLI auth works (gateway can run agent loops using subscription auth from the mounted `~/.codex`).
- [ ] Telegram message round-trip works end-to-end via the Dockerized gateway.
- [ ] `/agent` request from Telegram hits Codex via gateway → MCP → service → response back through Telegram.
- [ ] `./bin/openclaw config` outputs the working snippet for the chosen MCP transport.
- [ ] `MIGRATION_NOTES.md` has the Phase 1.5 section filled in.
- [ ] All existing tests pass.

**Stop and review with user before Phase 2.** After user confirms parity on the Dockerized setup, proceed to Phase 2 destructive deletions.

## Rollback safety net (before any Phase 2 work)

Phase 2 is destructive: it deletes `openclaw/worker/`, `jobhunter/telegram.py`, and `openclaw/workspace/` file-based IPC. Once those are gone, the *fast* rollback path is the previously-validated "both worlds coexist" commit, not git archaeology.

Before Phase 2 lands, the operator (or the migration runner) **must**:

1. Confirm the latest commit on the migration branch is the verified Phase 1.5 state (Telegram round-trip + MCP smoke test green).
2. Create an immutable tag at that commit:

   ```bash
   git tag -a pre-phase-2-YYYY-MM-DD -m "Phase 1.5 verified; both old + new stacks coexist"
   ```

3. Create an archive branch pointing at the same SHA, for ergonomic checkout:

   ```bash
   git branch archive/pre-phase-2 pre-phase-2-YYYY-MM-DD
   ```

4. Push tag + branch to `origin` so the rollback target survives local-machine loss:

   ```bash
   git push origin pre-phase-2-YYYY-MM-DD
   git push origin archive/pre-phase-2
   ```

5. Run Phase 2 on a *new* branch (`phase-2-cleanup`) cut from the migration branch — not directly on the migration branch. Merge only after Phase 2 acceptance.

### Rollback recipe

If Phase 2 introduces a regression that cannot be hotfixed inside its own branch:

```bash
git checkout archive/pre-phase-2
docker compose down
docker compose up -d jobhunter-service openclaw-gateway

# If the Python Telegram bot is also needed (legacy path):
#   docker compose up -d jobhunter        # the legacy container in pre-1.5 compose
```

The legacy Python Telegram bot (`jobhunter/telegram.py`) and the file-based agent IPC in `openclaw/workspace/` are still present in this archive. The OpenClaw gateway in this commit is the same Dockerized Phase 1.5 build, so Codex/MCP keeps working while the legacy path acts as a fallback.

Git history alone preserves the pre-migration baseline at `58efc80` (the commit before `540ff80 File OpenClaw migration epic`). That is the fully-pre-OpenClaw world — useful for reference, not as a daily rollback target since it predates the Python service refactor.

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
