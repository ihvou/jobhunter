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

## Phase 1.5c — Wire Codex app-server to Jobhunter MCP

**Status**: implemented on branch `openclaw-phase-1-5`; commit footer to be filled after commit.

The intended runtime remains OpenClaw's native Codex app-server harness:

```json5
agents: {
  defaults: {
    agentRuntime: { id: "codex" },
    model: { primary: "openai-codex/gpt-5.5" }
  }
}
```

Do **not** switch the main agent to `codex-cli` just to make MCP visible. `codex-cli` can expose MCP through Codex config overrides, but it is the CLI backend path, not the primary OpenClaw/Codex harness we want for Telegram, skills, native message tools, and long-lived channel sessions.

The missing piece was Codex's own MCP approval layer. OpenClaw can register the MCP server, and Codex can discover it, but a headless app-server turn will cancel/stall an MCP tool call unless the server/tool is configured as approved in Codex's per-agent config. `./bin/openclaw onboard` must write this into the OpenClaw volume:

```toml
[mcp_servers.jobhunter]
default_tools_approval_mode = "approve"
command = "python3"
args = ["-m", "jobhunter.openclaw_mcp"]
cwd = "/opt/jobhunter"

[mcp_servers.jobhunter.env]
JOBHUNTER_SERVICE_URL = "http://jobhunter-service:8765"
```

This is intentionally narrower than relaxing shell execution. Jobhunter MCP tools are bounded service endpoints; OpenClaw `group:runtime` and `group:fs` stay denied, native Codex shell is approval-gated with `appServer.approvalPolicy = "on-request"`, Codex app-server sandbox is `read-only`, and `/var/run/docker.sock` remains unmounted.

OpenClaw tool policy also belongs at top level:

```json5
tools: {
  profile: "messaging",
  alsoAllow: ["web_search", "web_fetch"],
  deny: ["group:runtime", "group:fs", "group:automation"],
  fs: { workspaceOnly: true },
  exec: { security: "deny", ask: "always" },
  elevated: { enabled: false }
}
```

Avoid `agents.defaults.tools` for this OpenClaw build; config validation rejects it. Avoid explicit `tools.allow: ["bundle-mcp", ...]`; `messaging` already carries the profile defaults, and this build logs explicit `bundle-mcp` as an unknown allowlist entry.

Also avoid `appServer.approvalPolicy = "never"` in this Dockerized setup. OpenClaw's `tools.exec.security = "deny"` blocks OpenClaw runtime exec tools, but it does not remove Codex's native shell tool from the Codex app-server harness. `on-request` keeps shell gated without blocking approved Jobhunter MCP tools.

MCP tool descriptions should not turn read calls into unconditional side effects. `jobhunter_get_more_jobs` supports diagnostics and agent analysis with `mark_sent=false`; the Telegram inline-button rendering contract applies only when the user is actually asking to receive a digest.

**Critical missing piece discovered during live verification.** Writing `[mcp_servers.jobhunter]` to Codex's `config.toml` is necessary but NOT sufficient with Codex CLI 0.128.0. Empirically, after a fresh gateway restart with only the config.toml block present, `codex mcp list` reports "No MCP servers configured yet" and the agent's toolset never includes the jobhunter tools. **The `codex mcp add` command must run separately** so Codex's own runtime registry marks the server enabled. `bin/openclaw onboard` now runs `register_codex_mcp_jobhunter` after `patch_codex_mcp_approval` to handle this.

Acceptance evidence (real, trajectory-verified):

- Live Telegram session `2bff21f4-1b57-4d26-b436-be85c2019661` (2026-05-14): user pressed "Get more jobs", agent emitted 5 `message` tool calls each with `presentation.blocks[].type=buttons` and 4 inline buttons; user confirmed buttons rendered on Telegram screen.
- Follow-up turn in the same session triggered the staleness self-heal: agent called `jobhunter_collect_all_sources` (pulling new Gmail alerts), then `jobhunter_get_more_jobs` again, and surfaced high-relevance product/AI roles (Product Lead Core Platform & AI, AI Product Owner — VC-backed GovTech, AI Product Engineer, Head of Product Toronto) as a fresh batch with inline buttons.
- Real MCP execution confirmed by absence of fabricated job rows: every job in the digest matched DB rows queryable via `/query-sql`, with company/title/url/score consistent with `jobs` table state at that timestamp.

The earlier internal "phase15c" diagnostic-only sessions did NOT exercise MCP — Codex CLI was returning fabricated row counts because `codex mcp add` had not yet been run. Verification done purely by chat output is unreliable; trajectories and the Codex `logs_2.sqlite` `mcp_tool_call_*` events are the only trustworthy signals.

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

## Phase 1.5b — Wire OpenClaw inline-keyboard rendering (MANDATORY before Phase 2)

**Why this exists**: Phase 1.5 verified Codex/MCP/Telegram round-trips work, but the digest sent by `Get more jobs` arrives as plain text with no per-job action buttons. The pre-migration `[Applied]`/`[Irrelevant]`/`[Snooze]`/`[Cover note]` buttons were built by `jobhunter/telegram.py`'s custom Python keyboard code. Phase 2 deletes that file. If we run Phase 2 before wiring OpenClaw's native inline-keyboard path, those buttons disappear — UX regression.

OpenClaw 2026.5.7 has native support for this. We just need to enable it and teach the agent to use it.

### 1.5b.1. Enable the channel capability

Apply via `openclaw config patch --stdin`:

```json
{
  "channels": {
    "telegram": {
      "capabilities": { "inlineButtons": "dm" },
      "actions": { "sendMessage": true }
    }
  }
}
```

Restart gateway. This exposes the generic `message` action tool to the agent with inline-button support, and gates callbacks to DMs only (matches our chat allowlist).

### 1.5b.2. Agent emit contract (the `message` tool)

When `actions.sendMessage: true` is set, the agent gets a `message` tool. Documented in OpenClaw's stock system prompt (`/app/dist/system-prompt-BIKbdIsV.js:303`):

```
action=send  target=<telegram_target>  message=<text>  buttons=[[{text,callback_data,style?}]]
```

- `buttons` is a 2D array: outer = rows, inner = buttons in row
- `style` (optional) is one of `primary` | `success` | `danger`
- `callback_data` must be ≤ 64 bytes (Telegram limit)
- Standard MarkdownV2/HTML rendering rules apply to `message`

### 1.5b.3. Callback dispatch (button taps → agent)

`/app/dist/bot-Ce301bOE.js:2450-2457` (verified): when the user taps a button, OpenClaw's bot handler:

1. Calls `answerCallbackQuery` to dismiss the spinner
2. Tries registered plugin handlers (none for us)
3. For unmatched callbacks, **injects `callback_data` as a synthetic user-text message into the same conversation** with `messageIdOverride: callback.id` and `forceWasMentioned: true`

So a tap on `[Applied]` with `callback_data: "applied:abc123"` arrives at the agent as if the user just typed `applied:abc123`. The agent does NOT receive a separate "callback event" — it sees a normal turn.

This means the SKILL.md must teach the agent to recognize the `<action>:<job_id>` text format and route to the corresponding MCP tool.

### 1.5b.4. callback_data scheme

Standardize on:

```
applied:<job_id_first_12>     → jobhunter_mark_job(id, status="applied")
irrelevant:<job_id_first_12>  → jobhunter_mark_job(id, status="irrelevant")
snooze:<job_id_first_12>      → jobhunter_mark_job(id, status="snoozed", snooze_days=1)
cover:<job_id_first_12>       → jobhunter_cover_note(id)
```

`<job_id_first_12>` is the first 12 chars of the SHA-256 `jobs.id` — fits comfortably under 64 bytes and is unique enough at our scale (1771 jobs → ~10⁻¹² collision). The MCP server resolves the 12-char prefix to the full job id via SQL LIKE.

### 1.5b.5. SKILL.md additions

Append to `skills/jobhunter/SKILL.md`:

```markdown
## Digest rendering with inline buttons

When the user asks for fresh jobs (e.g., "Get more jobs"), after calling
`jobhunter_get_more_jobs` you MUST emit the response via the OpenClaw `message`
tool with per-job inline buttons. Do NOT just return a text reply.

For each job in the returned shortlist, emit one `message` action with:

  target = "telegram:<chat_id_from_conversation_metadata>"
  message = "<rank>. <title> — <company> — score <total_score>\n<url>"
  buttons = [[
    { text: "Applied",    callback_data: "applied:<job_id_first_12>",    style: "success" },
    { text: "Irrelevant", callback_data: "irrelevant:<job_id_first_12>", style: "danger" },
    { text: "Snooze",     callback_data: "snooze:<job_id_first_12>" },
    { text: "Cover",      callback_data: "cover:<job_id_first_12>",      style: "primary" }
  ]]

### Callback dispatch (button taps)

When a user message arrives matching one of these patterns, treat it as a
button-tap callback (NOT free-form text) and route immediately:

  applied:<12_hex>     → call `jobhunter_mark_job(id_prefix=<12_hex>, status="applied")`
  irrelevant:<12_hex>  → call `jobhunter_mark_job(id_prefix=<12_hex>, status="irrelevant")`
  snooze:<12_hex>      → call `jobhunter_mark_job(id_prefix=<12_hex>, status="snoozed", snooze_days=1)`
  cover:<12_hex>       → call `jobhunter_cover_note(id_prefix=<12_hex>)` then reply with the draft

After successful action, do NOT post a verbose confirmation message; the user
already sees the button tap was acknowledged in Telegram. A one-line ack
("Marked as applied" / "Snoozed 24h") is fine.
```

### 1.5b.6. MCP server changes (`jobhunter/openclaw_mcp.py`)

`jobhunter_mark_job` already exists but currently takes a full job_id. Extend it to also accept `id_prefix` (first 12 hex), resolving via `WHERE id LIKE ?||'%'` against `jobs`. Same for `jobhunter_cover_note`. No new tools needed.

Add a small helper in `jobhunter/service.py` POST `/jobs/resolve_prefix` that maps a 12-char prefix to a full id (used by both MCP tools) — defensively reject if the prefix matches >1 row.

### 1.5b.7. Phase 1.5b acceptance

- [ ] `Get more jobs` returns a digest where each job has 4 inline buttons.
- [ ] Tapping `Applied` on a job inserts an `agent_actions` row with `kind='mark_job'`, `status='applied'`, the resolved full job_id, and an audit timestamp.
- [ ] Tapping `Irrelevant` marks the job irrelevant in `jobs.status`.
- [ ] Tapping `Snooze` sets `jobs.snoozed_until` 24h ahead.
- [ ] Tapping `Cover` returns a generated cover-note draft in chat.
- [ ] After any tap, the original digest message either keeps its buttons (Telegram only allows one mutation per callback; document the behavior) OR shows a brief "✓ <action>" inline acknowledgment.
- [ ] `docker compose --profile openclaw config --quiet` still passes.

**Stop and verify with operator before Phase 2.** Phase 2 deletes `jobhunter/telegram.py`. If any 1.5b acceptance test fails, Phase 2 must NOT proceed.

## Phase 2 — Retire custom Telegram + worker code (2-3 days)

After Phase 1 / 1.5 / 1.5b / 1.5c parity is proven.

### Updates folded in from 1.5b/1.5c findings (read before executing)

The original Phase 2 spec was written before we discovered the Codex CLI / OpenClaw integration quirks. The instructions below override or supplement the per-step text:

1. **Do NOT remove `openclaw-gateway` from `docker-compose.yml` in 2a.** That instruction referred to the *legacy* worker container that no longer exists. The current `openclaw-gateway` IS the real OpenClaw runtime container from Phase 1.5 and must stay.
2. **MCP registration requires three artifacts**, not just config files. `bin/openclaw onboard` must call all three or the bot is silently broken:
   - `openclaw config patch` writing `mcp.servers.jobhunter` into `openclaw.json`
   - Python helper writing `[mcp_servers.jobhunter]` with `default_tools_approval_mode = "approve"` into `codex-home/config.toml`
   - `codex mcp add jobhunter -- python3 -m jobhunter.openclaw_mcp` (the Codex CLI registry update — config.toml alone is not picked up by Codex CLI 0.128.0)
3. **OpenClaw tool policy lives at top-level `tools.*`, not under `agents.defaults.tools.*`.** The latter is rejected by schema validation in this build. Use `tools.profile = "messaging"` plus `tools.alsoAllow = ["web_search", "web_fetch"]`; do not put `bundle-mcp` in any allowlist (it's logged as unknown).
4. **Codex's native bash tool is NOT gated by `tools.exec.security = "deny"`.** That setting blocks OpenClaw's runtime exec tools only. Codex app-server requires `appServer.approvalPolicy = "on-request"` plus `sandbox = "read-only"` to make its own shell approval-gated. Both must be in `plugins.entries.codex.config.appServer`.
5. **SKILL.md is loaded lazily and the agent often skips it.** Authoritative rendering rules, callback dispatch, and staleness behavior must live in MCP tool descriptions (where the agent reads them inline with each call), not in SKILL.md. SKILL.md is duplicate documentation, not the source of truth.
6. **Inline buttons render via `presentation.blocks[].buttons`**, not a direct `buttons` arg on the `message` tool. The agent emits `{action: "send", target, message, presentation: {blocks: [{type: "buttons", buttons: [...]}]}}` and OpenClaw's Telegram channel converts that to `reply_markup.inline_keyboard`.
7. **No native reply-keyboard support.** OpenClaw renders inline-keyboards (per-message buttons) but does not maintain a persistent bottom-pane reply-keyboard like the pre-migration `jobhunter/telegram.py`. The four "Get more jobs / Update sources / Tune scoring / Usage" reply-keyboard surface labels become free-text triggers — the agent routes them by intent. Optionally register `channels.telegram.customCommands` for `/jobs`, `/sources`, `/scoring`, `/usage` slash commands.
8. **Telegram polling in Docker requires `network.autoSelectFamily: false` + `dnsResultOrder: "ipv4first"`.** Without these, getUpdates long-poll stalls for 5-9 minutes at a time under Docker bridge networking. Already in current openclaw.json; must be in `bin/openclaw onboard`.
9. **Verification must be trajectory-based, never chat-output-based.** Codex/gpt-5.5 will fabricate plausible answers when MCP tools aren't actually connected. Real acceptance evidence is `tool.call name=jobhunter_*` events in `~/.openclaw/agents/main/sessions/*.trajectory.jsonl` plus `mcp_tool_call_begin`/`mcp_tool_call_end` rows in `~/.openclaw/agents/main/agent/codex-home/logs_2.sqlite`.
10. **Docker healthcheck for openclaw-gateway must be permissive** (60s interval, 15s timeout, 5 retries). The default would kill the gateway on Codex rate-limit event-loop hangs. Already in `docker-compose.yml`.

### 2a. Delete `openclaw/worker/`

- Remove the entire `openclaw/worker/` directory.
- **Skip** the original "remove the openclaw-gateway service" instruction — that text referred to a legacy container that no longer exists.
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

**Trajectory-verified, not chat-verified.** For every acceptance check below, the corresponding `tool.call` event must appear in `/home/node/.openclaw/agents/main/sessions/*.trajectory.jsonl` AND (where applicable) `mcp_tool_call_begin` / `mcp_tool_call_end` must appear in `~/.openclaw/agents/main/agent/codex-home/logs_2.sqlite`. Chat output alone is not acceptance evidence.

**Container state:**
- `docker ps` shows exactly two containers: `jobhunter-service` and `openclaw-gateway` (both healthy). The legacy custom Node worker container is gone.
- No `openclaw/workspace/` mount remains; no `jobhunter/telegram.py` polling-task running.

**Functional checks (each via a real Telegram round-trip with trajectory inspection):**
- "Get more jobs" — agent calls `jobhunter_get_more_jobs`; if `queue_is_stale` is true, also calls `jobhunter_collect_all_sources` first; renders each job via per-job `message` action with `presentation.blocks` buttons.
- "Update sources" — agent calls `jobhunter_propose_actions` with kind `sources_proposal`; bot returns proposal ids; on user approval, agent calls `jobhunter_apply_action`; `sources.local.json` is updated and an `agent_actions` audit row appears.
- "Tune scoring" — same flow as sources, with kind `scoring_rule_proposal` and `scoring.local.json` as the target.
- "Usage" — agent calls `jobhunter_usage`; bot returns formatted spend/quota/counters.
- Inline button taps: tapping `Applied`/`Irrelevant`/`Snooze`/`Cover` on a digest job emits `callback_data` like `applied:<id_prefix>`; agent receives that as a synthetic user text turn (per OpenClaw `bot.on("callback_query")` dispatch) and calls `jobhunter_mark_job` / `jobhunter_cover_note` accordingly; DB rows update.
- `/agent <free text>` — generic free-form path still works; agent routes to MCP tools or composes a direct reply as appropriate.
- `/history`, `/revert` — slash-command equivalents reach `jobhunter_history` / `jobhunter_revert_action`.

**Negative checks:**
- Codex internal log shows no `bash` calls during a clean digest turn (only MCP + message tool calls).
- No `jobhunter/telegram.py` import errors in any test run.
- `docker compose --profile openclaw config --quiet` passes.
- `python3 -m unittest discover -s tests` passes.

**Operational checks (24h soak after merge):**
- Zero `Polling stall detected` events in the gateway log.
- Container memory steady (no climb past 1GiB / 2GiB limit).
- No unexpected gateway self-restarts triggered by `rateLimits/updated` notifications.
- At least one successful background staleness self-heal: a "Get more jobs" hit with stale queue → automatic `collect_all_sources` → re-rendered digest with higher scores.

### Phase 2 implementation notes

- `jobhunter-service` is the only Python runtime container. It exposes HTTP endpoints for health, digest, collection, action proposals/apply/revert/history, usage, and bounded job mutations.
- Telegram polling, message rendering, callback routing, and Codex sessions are owned by `openclaw-gateway`.
- The old file-based `openclaw/workspace/` IPC and `AgentCoordinator` loop are gone. Agent requests now flow through OpenClaw's Codex runtime into Jobhunter MCP tools.
- `plugins/jobhunter-tools/` is loaded by `./bin/openclaw onboard` so Jobhunter actions are exposed as OpenClaw dynamic tools for trajectory-visible `tool.call` evidence.
- Codex-native MCP remains registered through top-level `mcp.servers`, per-agent `config.toml`, and `codex mcp add`; it provides Codex-side tool access and `mcp_tool_call_*` evidence, but it is not sufficient by itself for OpenClaw trajectory-visible tool calls.
- `tools.alsoAllow` includes `jobhunter-tools` specifically. Do not replace this with `group:plugins`; the point is to expose only the Jobhunter bridge plus messaging/web while runtime/fs/automation stay denied.
- Phase 2 verification uses OpenClaw dynamic tool trajectories as the primary runtime signal. In Codex 0.128.0, native MCP calls appear in `logs_2.sqlite` as `mcp__jobhunter__...` with `mcp_tool=true` and `rmcp::service ... CallToolRequest ...`; the logs did not contain literal `mcp_tool_call_begin` / `mcp_tool_call_end` strings.
- `./bin/jobhunter` is a compatibility wrapper for `./bin/openclaw` for one release.
- Done at `5844bd0`.

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
