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

The `openclaw-gateway` and `jobhunter-service` containers share Docker's default bridge network, so `http://jobhunter-service:8765` resolves between them. Phase 2 uses the `jobhunter-tools` OpenClaw plugin as the sole tool surface; the earlier Python stdio MCP bridge was retired after duplicate-tool review.

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
- `config` prints the plugin-based OpenClaw snippet with container-relative paths.

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

## Phase 1.5c — Historical Codex-native MCP investigation

**Status**: implemented during Phase 1.5, then retired after Phase 2 follow-up review.

The intended runtime remains OpenClaw's native Codex app-server harness:

```json5
agents: {
  defaults: {
    agentRuntime: { id: "codex" },
    model: { primary: "openai-codex/gpt-5.5" }
  }
}
```

The main agent remains OpenClaw's native Codex app-server harness. Do **not** switch the main agent to `codex-cli`; that is a CLI backend path, not the primary OpenClaw/Codex harness we want for Telegram, skills, native message tools, and long-lived channel sessions.

During 1.5c we proved Codex-native MCP could be wired, but Phase 2 acceptance showed the OpenClaw dynamic plugin is the tool surface that gives the required trajectory-visible `tool.call name=jobhunter_*` events. The duplicate Codex-native MCP path is now retired. `mcp.servers.jobhunter`, Codex `[mcp_servers.jobhunter]`, and `codex mcp add jobhunter` are **not used**.

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

Also avoid `appServer.approvalPolicy = "never"` in this Dockerized setup. OpenClaw's `tools.exec.security = "deny"` blocks OpenClaw runtime exec tools, but it does not remove Codex's native shell tool from the Codex app-server harness. `on-request` keeps shell gated.

Plugin tool descriptions should not turn read calls into unconditional side effects. `jobhunter_get_more_jobs` supports diagnostics and agent analysis with `mark_sent=false`; the Telegram inline-button rendering contract applies only when the user is actually asking to receive a digest.

Acceptance evidence (real, trajectory-verified):

- Live Telegram session `2bff21f4-1b57-4d26-b436-be85c2019661` (2026-05-14): user pressed "Get more jobs", agent emitted 5 `message` tool calls each with `presentation.blocks[].type=buttons` and 4 inline buttons; user confirmed buttons rendered on Telegram screen.
- Follow-up turn in the same session triggered the staleness self-heal: agent called `jobhunter_collect_all_sources` (pulling new Gmail alerts), then `jobhunter_get_more_jobs` again, and surfaced high-relevance product/AI roles (Product Lead Core Platform & AI, AI Product Owner — VC-backed GovTech, AI Product Engineer, Head of Product Toronto) as a fresh batch with inline buttons.
- Real MCP execution confirmed by absence of fabricated job rows: every job in the digest matched DB rows queryable via `/query-sql`, with company/title/url/score consistent with `jobs` table state at that timestamp.

The earlier internal "phase15c" diagnostic-only sessions did NOT exercise real Jobhunter tools. Verification done purely by chat output is unreliable; OpenClaw trajectory `tool.call` events are the trustworthy signal.

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

`<job_id_first_12>` is the first 12 chars of the SHA-256 `jobs.id` — fits comfortably under 64 bytes and is unique enough at our scale (1771 jobs → ~10⁻¹² collision). The service resolves the 12-char prefix to the full job id via SQL LIKE.

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

### 1.5b.6. Tool callback changes

`jobhunter_mark_job` already exists but currently takes a full job_id. Extend it to also accept `id_prefix` (first 12 hex), resolving via `WHERE id LIKE ?||'%'` against `jobs`. Same for `jobhunter_cover_note`. No new tools needed.

Add a small helper in `jobhunter/service.py` POST `/jobs/resolve_prefix` that maps a 12-char prefix to a full id — defensively reject if the prefix matches >1 row.

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
2. **Jobhunter tools are exposed solely through the OpenClaw plugin.** `bin/openclaw onboard` loads `plugins/jobhunter-tools/` and keeps `tools.alsoAllow` narrowed to `["web_search", "web_fetch", "jobhunter-tools"]`. `mcp.servers.jobhunter`, Codex `[mcp_servers.jobhunter]`, and `codex mcp add jobhunter` are NOT used.
3. **OpenClaw tool policy lives at top-level `tools.*`, not under `agents.defaults.tools.*`.** The latter is rejected by schema validation in this build. Use `tools.profile = "messaging"` plus `tools.alsoAllow = ["web_search", "web_fetch"]`; do not put `bundle-mcp` in any allowlist (it's logged as unknown).
4. **Codex's native bash tool is NOT gated by `tools.exec.security = "deny"`.** That setting blocks OpenClaw's runtime exec tools only. Codex app-server requires `appServer.approvalPolicy = "on-request"` plus `sandbox = "read-only"` to make its own shell approval-gated. Both must be in `plugins.entries.codex.config.appServer`.
5. **SKILL.md is loaded lazily and the agent often skips it.** Authoritative rendering rules, callback dispatch, and staleness behavior must live in plugin tool descriptions (where the agent reads them inline with each call), not in SKILL.md. SKILL.md is duplicate documentation, not the source of truth.
6. **Inline buttons render via `presentation.blocks[].buttons`**, not a direct `buttons` arg on the `message` tool. The agent emits `{action: "send", target, message, presentation: {blocks: [{type: "buttons", buttons: [...]}]}}` and OpenClaw's Telegram channel converts that to `reply_markup.inline_keyboard`.
7. **No native reply-keyboard support.** OpenClaw renders inline-keyboards (per-message buttons) but does not maintain a persistent bottom-pane reply-keyboard like the pre-migration `jobhunter/telegram.py`. The four "Get more jobs / Update sources / Tune scoring / Usage" reply-keyboard surface labels become free-text triggers — the agent routes them by intent. Optionally register `channels.telegram.customCommands` for `/jobs`, `/sources`, `/scoring`, `/usage` slash commands.
8. **Telegram polling in Docker requires `network.autoSelectFamily: false` + `dnsResultOrder: "ipv4first"`.** Without these, getUpdates long-poll stalls for 5-9 minutes at a time under Docker bridge networking. Already in current openclaw.json; must be in `bin/openclaw onboard`.
9. **Verification must be trajectory-based, never chat-output-based.** Codex/gpt-5.5 will fabricate plausible answers when tools aren't actually connected. Real acceptance evidence is bare `tool.call name=jobhunter_*` events in `~/.openclaw/agents/main/sessions/*.trajectory.jsonl`.
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

**Trajectory-verified, not chat-verified.** For every acceptance check below, the corresponding bare `tool.call name=jobhunter_*` event must appear in `/home/node/.openclaw/agents/main/sessions/*.trajectory.jsonl`. Chat output alone is not acceptance evidence.

**Container state:**
- `docker ps` shows exactly two containers: `jobhunter-service` and `openclaw-gateway` (both healthy). The legacy custom Node worker container is gone.
- No `openclaw/workspace/` mount remains; no `jobhunter/telegram.py` polling-task running.

**Functional checks (each via a real Telegram round-trip with trajectory inspection):**
- "Get more jobs" — agent calls `jobhunter_get_more_jobs`; if `queue_is_stale` is true, also calls `jobhunter_collect_all_sources` first; renders each job via per-job `message` action with `presentation.blocks` buttons.
- "Update sources" — agent calls `jobhunter_propose_actions` with kind `sources_proposal`; bot returns proposal ids; on user approval, agent calls `jobhunter_apply_action`; `sources.local.json` is updated and an `agent_actions` audit row appears.
- "Tune scoring" — same flow as sources, with kind `scoring_rule_proposal` and `scoring.local.json` as the target.
- "Usage" — agent calls `jobhunter_usage`; bot returns formatted spend/quota/counters.
- Inline button taps: tapping `Applied`/`Irrelevant`/`Snooze`/`Cover` on a digest job emits `callback_data` like `applied:<id_prefix>`; agent receives that as a synthetic user text turn (per OpenClaw `bot.on("callback_query")` dispatch) and calls `jobhunter_mark_job` / `jobhunter_cover_note` accordingly; DB rows update.
- `/agent <free text>` — generic free-form path still works; agent routes to Jobhunter plugin tools or composes a direct reply as appropriate.
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
- The old file-based `openclaw/workspace/` IPC and `AgentCoordinator` loop are gone. Agent requests now flow through OpenClaw's Codex runtime into Jobhunter plugin tools.
- `plugins/jobhunter-tools/` is loaded by `./bin/openclaw onboard` so Jobhunter actions are exposed as OpenClaw dynamic tools for trajectory-visible `tool.call` evidence.
- Jobhunter tools are exposed solely through the `jobhunter-tools` OpenClaw plugin. `mcp.servers.jobhunter` and `codex mcp add` are NOT used.
- `tools.alsoAllow` includes `jobhunter-tools` specifically. Do not replace this with `group:plugins`; the point is to expose only the Jobhunter bridge plus messaging/web while runtime/fs/automation stay denied.
- Phase 2 verification uses OpenClaw dynamic tool trajectories as the runtime signal: bare `tool.call name=jobhunter_*` events plus successful `tool.result` rows.
- `./bin/jobhunter` is a compatibility wrapper for `./bin/openclaw` for one release.
- Done at `5844bd0` and trajectory bridge completed at `7d432d5`.

## Phase 3 — Plug in OpenClaw's tool ecosystem

The full Phase 3 vision in the original spec covered firecrawl, exa, agenticmail, Gmail Pub/Sub, and source-validation. Based on Phase 1.5b/1.5c/2 learnings, Phase 3 is split into two runs to keep each Codex session under 4h and reduce blast radius:

- **Phase 3a (this section)** — firecrawl + exa + plugin-allowlist tightening + email-alert parser fix. 3–4 hours of Codex work.
- **Phase 3b (deferred)** — Gmail Pub/Sub webhook + `agenticmail` skill. Needs Google Cloud setup, OAuth scopes, public webhook URL — too heavy for a single session. Defer until Phase 3a is stable and the user has GCP project provisioned.

### Lessons folded in from 1.5b/1.5c/2 (read before executing 3a)

1. **Plugin tools, not Codex-native MCP.** Phase 2 (commit 718c451) retired the Codex-native MCP path. New plugins install via OpenClaw's plugin registry, NOT via `codex mcp add`.
2. **Tool descriptions are the only reliable instruction surface.** SKILL.md is loaded lazily and the agent often skips it. Put firecrawl/exa routing rules in the plugin tool descriptions, NOT only in SKILL.md.
3. **`tools.profile: "messaging"` is narrow.** Every new plugin id must be added to `tools.alsoAllow` or its tools stay invisible to the agent.
4. **Verification is trajectory-based, never chat-based.** Codex/gpt-5.5 will fabricate plausible answers when tools aren't actually wired. Real acceptance evidence is `tool.call name=firecrawl_*` events in `/home/node/.openclaw/agents/main/sessions/*.trajectory.jsonl`.
5. **Codex's native bash is gated by `appServer.approvalPolicy: on-request`**, not `tools.exec.security: deny`. Both stay.
6. **`plugins.allow` was empty** during Phase 2 verification, which let 4 plugins (browser, phone-control, talk-voice, device-pair) auto-load that we never asked for. Phase 3a sets an EXPLICIT allowlist.
7. **API keys**: firecrawl and exa require keys. Free tiers are sufficient for jobhunter scale. Plugin must refuse to load gracefully if its key is missing; don't fake-install.

### 3a.1. Install firecrawl plugin

- Install via OpenClaw's plugin install mechanism. Pin the version explicitly (no `@latest` — supply-chain risk per the existing audit warning).
- Add `FIRECRAWL_API_KEY` to `docker-compose.yml` under `openclaw-gateway.environment` as `${FIRECRAWL_API_KEY:-}`.
- Document in `.env.example` that the user needs to provide this for Phase 3a.
- Configure conservative limits (small max page size, low concurrency) to stay inside the free tier.
- If the key is missing, plugin should refuse to load gracefully; document the failure mode in `MIGRATION_NOTES.md`.

Acceptance: `node dist/index.js config get plugins.entries.firecrawl` returns the configured entry; gateway restart shows firecrawl in the loaded plugins list.

### 3a.2. Install exa plugin

Same pattern as firecrawl: pin version, `EXA_API_KEY` env var, conservative limits, graceful fallback if key missing.

Acceptance: same shape as firecrawl.

### 3a.3. Tighten plugins.allow + alsoAllow

In `bin/openclaw` (the `patch_jobhunter_openclaw_config` payload) AND `config/openclaw.example.json5`:

```json
{
  "plugins": {
    "allow": ["codex", "telegram", "jobhunter-tools", "firecrawl", "exa", "memory-core"],
    "entries": { /* existing entries plus enabled: true for firecrawl and exa */ }
  },
  "tools": {
    "alsoAllow": ["web_search", "web_fetch", "jobhunter-tools", "firecrawl", "exa"]
  }
}
```

Acceptance: gateway restart logs `http server listening (6 plugins: codex, exa, firecrawl, jobhunter-tools, memory-core, telegram; ...)` or document why an extra plugin must be retained. The `plugins.allow is empty; discovered non-bundled plugins may auto-load` startup warning must disappear.

### 3a.4. Update jobhunter-tools tool descriptions for firecrawl/exa awareness

In `plugins/jobhunter-tools/index.js`, update `jobhunter_propose_actions` description to mention:

- "If `web_fetch` returns 403/404 on a candidate source URL, retry once with `firecrawl` before defaulting to `source_type="community"` + `status="test"`."
- "For 'find me sources for X' requests, use `exa` to search first, then propose the top results via `jobhunter_propose_actions`."

Do NOT add this guidance to SKILL.md only — see lesson 2 above.

Add regression-guard assertions in `plugins/jobhunter-tools/tests/index.test.js` that the propose_actions description contains the substrings `"firecrawl"` and `"exa"`.

### 3a.5. Fix email-alert parser noise

`email-job-alerts` source is producing wrapper rows that should never have been parsed as jobs. Examples from the live DB:
- `"Read more"` (LinkedIn link text)
- `"30+ new jobs match your preferences"` (LinkedIn footer)
- `"Top job picks for you"` (LinkedIn header)
- Rows with `length(title) < 8`

In `jobhunter/sources.py`'s IMAP/email parser, add a post-parse filter that drops these patterns BEFORE insertion. Conservative keyword list — false positives mean dropping real jobs.

Add a test in `tests/test_sources.py` that constructs a fake LinkedIn email chunk with one real-looking job + the wrapper strings and asserts only the real job survives.

Provide a one-shot operator SQL in MIGRATION_NOTES.md to clean existing noise rows:

```sql
UPDATE jobs SET status='irrelevant'
WHERE source_id='email-job-alerts'
  AND (title='Read more' OR title LIKE '%new jobs match%'
       OR title LIKE '%Top job picks%' OR length(title) < 8)
  AND status='new'
```

This is run once via `jobhunter_query_sql` after deploy. The agent should not run it autonomously.

### 3a.6. Phase 3a acceptance (the DOU end-to-end test)

Phase 1.5b surfaced that DOU is unreachable from the Docker container because Cloudflare/geo-blocks 403 the bare `web_fetch`. Phase 3a should fix that via firecrawl. Verify via a real Telegram round-trip:

1. User sends `/agent please add https://jobs.dou.ua/vacancies/?category=Product%20Manager&from=maybe to sources`.
2. Agent uses firecrawl on the URL → succeeds → calls `jobhunter_propose_actions` with `kind=sources_proposal`, `type=community` (or `rss` if a feed is discovered), `status=test` → emits message asking for approval.
3. User replies `approve <action_id>`.
4. Agent calls `jobhunter_apply_action` → success → emits confirmation message.
5. Trigger a collection. Verify ≥1 DOU job in the `jobs` table with non-zero score.

Acceptance evidence to paste in the commit message:
- Telegram session id where the DOU propose→approve→apply succeeded
- Trajectory excerpt with `tool.call name=firecrawl_*` event
- SQL count of DOU jobs in `jobs` table after collection

**If firecrawl also can't fetch DOU** (paid tier required, or DOU blocks firecrawl too), document the constraint in `MIGRATION_NOTES.md` and accept that DOU moves to Phase 3b's agenda. Do NOT hack `validate_source_row` to bypass the reachability probe — softening it is a separate decision for a separate change.

### 3a — out of scope

- Gmail Pub/Sub webhook (Phase 3b)
- agenticmail skill install (Phase 3b, after confirming the parser fix doesn't make it unnecessary)
- L1 scoring rule patches (the user drives these via the propose-approve-apply flow in the bot — that flow is already proven)
- Soft-mode reachability check for `status=test` sources (separate change, not in scope)

### 3a — verification before declaring done

```bash
python3 -m unittest discover -s tests
(cd plugins/jobhunter-tools && node --test tests/index.test.js)
docker compose --profile openclaw config --quiet
git diff --check
git status -sb
```

All must pass. Then the DOU Telegram smoke test (§3a.6).

### 3a — time budget

4 hours of Codex work. Stop and write up blockers at 4h regardless of completeness. Partial progress is fine; hallucinated "done" is not.

### 3a — branch and merge policy

- Cut a NEW branch `phase-3a-firecrawl-exa` from `phase-2-cleanup` at HEAD.
- Do not touch `phase-2-cleanup`, `openclaw-phase-1-5`, or `main`.
- After all commits land and Phase 3a acceptance passes, push the branch to `origin` and report final SHA. The user reviews and decides when to merge.

### Phase 3b — Firecrawl-backed community sources and email trigger bridge

Phase 3b keeps the GCP/OAuth setup as an operator-controlled step, but wires the local software surface needed for it:

- **Firecrawl-backed source validation**: when a `sources_proposal` is applied, community sources are checked by direct HEAD/GET first and by bounded Firecrawl scrape second. RSS/API/ATS sources still use the direct probe only.
- **Firecrawl-backed community collection**: community pages that direct HTTP cannot fetch are retried through Firecrawl. Markdown links from Firecrawl are parsed into normal job rows and scored through the existing L1/L2 path.
- **Gmail Pub/Sub bridge**: `jobhunter-service` exposes `POST /email/process`, and the OpenClaw plugin exposes `jobhunter_process_email(sender, subject, body, message_id?, date?, source_id?)`. Gmail hooks or a future email skill should pass parsed email content to that tool.
- **`agenticmail` decision**: the pinned OpenClaw image does not include an `agenticmail` plugin/skill. Keep the existing parser DSL and feed it from hooks for now; if a real agenticmail package is later adopted, it should call `jobhunter_process_email` instead of writing directly to the database.
- **Operator setup still required**: run OpenClaw's Gmail Pub/Sub setup only after providing the GCP project/OAuth/public webhook details, for example `openclaw webhooks gmail setup --account <account>`. Do not enable a public hook without a dedicated token and trusted ingress.

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

### Phase 4 implementation notes

- OpenClaw config enables cron, and `./bin/openclaw onboard` / `./bin/openclaw cron-install` register three CLI-backed jobs: `jobs-collection`, `jobs-rescore-on-feedback-change`, and `jobs-discovery-monthly`. They all route through the `jobs` agent and plugin tools; no shell or filesystem access is used.
- `agents.list` now defines `jobs` and `leads` agents. Both still share the same Dockerized gateway and the same `jobhunter-service`; domain separation comes from skill instructions and tool descriptions.
- `plugins/jobhunter-tools/` now exposes leadhunter tools in the same trajectory-visible plugin surface: `leadhunter_get_more_leads`, `leadhunter_save_leads`, `leadhunter_add_lead_source`, `leadhunter_mark_lead`, and `leadhunter_draft_pitch`.
- SQLite schema v11 adds `leads`, `lead_sources`, `lead_feedback`, and `lead_drafts`. Lead candidates must have a public URL, pass the same SSRF-safe URL validation, and are stored only when the user has approved the candidate list.
- `input/icp.local.md` is the private ICP input for lead research and pitch drafting. It is gitignored like the profile and CV.
- Outreach remains manual: lead pitch tools draft copy-paste text only and never send email, LinkedIn messages, or any automatic outreach.

## Phase 5 — UX polish: persistent keyboard, callback message_id, parser fixes

Three items surfaced during Phase 4 live testing. Each is a focused chunk of work and can be merged independently.

### 5a. Persistent reply-keyboard at the bottom of the chat

Phase 4 added `customCommands` slash entries (visible in Telegram's `/` menu), but the user wants a *persistent reply-keyboard* — the 2x2 button surface that sits below the message input field and is always visible. The four buttons are:

| | |
|---|---|
| `Get more jobs` | `My job profile` |
| `Get more leads` | `My ICP profile` |

Behavior:
- `Get more jobs` — triggers `jobhunter_get_more_jobs` (with staleness self-heal as documented)
- `My job profile` — bot replies with the current contents of `input/profile.local.md` (uses the existing `jobhunter_query_sql` against `candidate_profile`, OR reads the file via a new `jobhunter_show_profile` tool wrapping a service `/profile/show` endpoint)
- `Get more leads` — triggers `leadhunter_get_more_leads`
- `My ICP profile` — bot replies with the current contents of `input/icp.local.md` (new `leadhunter_show_icp` tool wrapping a service `/leads/icp/show` endpoint)

Implementation paths to investigate:
1. **OpenClaw `channels.telegram` config** — re-check the schema for a `replyKeyboard` / `persistentKeyboard` block. Earlier Phase 1.5b survey didn't find one but the schema may have been extended.
2. **Per-message reply_markup.keyboard** — attach `reply_markup: { keyboard: [[...]], resize_keyboard: true, persistent: true }` to every outgoing message via the `presentation` block. Investigate if `presentation` supports a `keyboard` block type (vs the existing `buttons` block for inline). If not, plugin-level workaround: extend the `jobhunter-tools` plugin's `message` action wrapper to inject the reply-keyboard markup.
3. **setMyCommands fallback** — not equivalent UX (slash menu vs always-on keyboard) so do not substitute.

Acceptance: after sending any agent reply, the 2x2 keyboard is visible at the bottom of the chat. Tapping `Get more jobs` triggers the digest. Tapping `My job profile` returns the profile contents in chat. Tapping `My ICP profile` returns the ICP contents. Tapping `Get more leads` triggers the leads digest.

### 5b. Callback message_id workaround — make delete/edit on tap actually work

Phase 4 live testing exposed: OpenClaw 2026.5.7 routes `callback_query` taps to the agent as a synthetic user message but uses `callback.id` (the Telegram callback_query identifier) as the synthetic `message_id`, NOT `callback.message.message_id` (the message that had the buttons). Result: `message(action="delete", messageId=...)` and `message(action="edit", messageId=...)` cannot target the original digest message — Telegram returns `Bad Request: message identifier is not specified`.

Two-call workaround (this phase implements it):

1. Agent emits each digest message via `message({action: "send", target, message, presentation: {blocks: [{type: "buttons", buttons: [PLACEHOLDER_BUTTONS]}]}})`. The placeholder buttons have a temporary `callback_data` like `pending:<id_prefix>`.
2. The send returns `{ok: true, messageId: "913", chatId: "855127987"}` — capture the real Telegram `messageId`.
3. Agent immediately emits a second call `message({action: "edit", messageId: "913", target, ...})` with the REAL buttons whose `callback_data` encodes the Telegram message_id: `applied:<id_prefix>:913` etc.
4. When the user taps `[Applied]`, the synthetic callback prompt now contains both the `id_prefix` AND the Telegram message_id. Agent parses both from `callback_data`, calls `jobhunter_mark_job`, then calls `message(action="delete", messageId="913", target)` — Telegram accepts because the id is now correct.

Cost: doubles the API calls per digest item (10-job digest = 20 calls instead of 10). Latency increases by ~10s on slower connections. Acceptable for the UX win.

Alternative path also evaluated:
- **Upstream OpenClaw issue**: ask for `callback_origin_message_id` in synthetic prompt metadata, or a `message(action="delete-callback-source")` tool that uses implicit context. File the issue; track separately. If upstream lands first, retire the two-call pattern.

Acceptance: tapping `[Applied]` / `[Irrelevant]` / `[Snooze]` (or the lead equivalents) on a digest item **removes that message from chat**. Confirmation message no longer needed (the disappearance IS the confirmation). DB updates audited as today.

### 5c. Per-source parser fix — addresses task candidate #3

Email-alert wrapper noise was solved in Phase 3a. Several other parser gaps remain, surfaced during Phase 4 digest review:

- **YC (`yc-jobs-product-manager-remote`)**: parser is grabbing company-description headers (`Confido (S21) • AI-enabled financial automation`) as job titles. Every YC row has `company="Unknown company"` because the company slug from the URL path (`/companies/<slug>/jobs/<id>`) isn't being extracted.
- **DOU (`dou-product-manager`)**: same `company="Unknown company"` problem. URL pattern `/companies/<slug>/vacancies/<id>` should yield the company slug.
- **WeWorkRemotely**: `"Company: Title"` prefix in titles (e.g., `"Instacart: Principal Product Manager"`). Strip company prefix when the company field is already populated.
- **LinkedIn email alerts**: template artifacts in titles (`"role at X is available"`) and company fields (`"X is available LinkedIn"`).

Implementation: source-specific parser functions in `jobhunter/sources.py`, one per problematic source, with fixture tests in `tests/test_sources.py` (HTML snippet → expected `{title, company, location, ...}`).

Acceptance: a fresh `/jobs` digest after this lands shows real company names for YC, DOU, WeWorkRemotely rows; LinkedIn titles no longer have `"role at X is available"` artifacts; YC and DOU rows have non-`"Unknown company"` company fields.

### 5d. Time and branch policy

Each sub-phase is independent and can be merged separately. Suggested order:
1. **5b** (two-call digest) — unblocks the delete UX users complain about. ~3-4h Codex.
2. **5a** (persistent reply-keyboard) — quality-of-life. ~2-3h Codex. Depends on schema investigation.
3. **5c** (parser fixes) — already filed as task candidate #3. ~3-4h Codex. Independent of 5a/5b.

All sub-phases on dedicated branches off `codex/phase-4-recurring-leadhunter` (or its merged successor). Verification is trajectory-based, same discipline as Phase 3a/3b/4.

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
