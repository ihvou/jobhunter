# Migration Notes

## Decisions

- Implement Phase 1 as a compatibility bridge first. Full Phase 2 deletion is deferred until real OpenClaw is installed, configured, and proven against Telegram.
- Use a stdlib HTTP `jobhunter-service` plus stdio MCP bridge instead of speculative declarative YAML tools. Upstream OpenClaw docs describe skills as instructions, not executable tool registrations.
- Keep the legacy Telegram bot and custom worker in place during this commit. They remain the rollback path until OpenClaw parity is verified.
- Publish the Python service on `127.0.0.1:8765` from Docker. This is loopback-only, not externally reachable, and lets a host-native OpenClaw gateway call the service on macOS.
- Keep the service's `config/` and `input/` mounts writable. OpenClaw itself does not receive broad filesystem write access; approved writes go through bounded `jobhunter-service` action endpoints that archive and audit changes.

## Spec Discrepancies

- `docs/automation/gmail-pubsub.md` currently resolves as an empty/placeholder doc in the upstream repo. Gmail Pub/Sub migration should not be implemented until the concrete schema and hook flow are verified.
- The proposed `skills/jobhunter/tools/*.yml` HTTP declaration format is not validated by upstream docs. The implemented path uses MCP, which OpenClaw documents as a first-class tool registry.
- OpenClaw sandbox/skill ecosystem has open community issues around sandbox skill paths and writable skills. Jobhunter keeps privileged writes behind bounded Python service endpoints to avoid relying on broad agent filesystem access.

## Deferred Approval Steps

Run these only after reviewing this migration branch:

```bash
git checkout -b openclaw-migration
npm install -g openclaw@latest
openclaw onboard --install-daemon
openclaw doctor
```

Then configure OpenClaw with:

- `skills.load.extraDirs` pointing at this repo's `skills/` directory, or copy `skills/jobhunter` into the active OpenClaw workspace `skills/`.
- An MCP server named `jobhunter` running `python3 -m jobhunter.openclaw_mcp` from this repo root.
- Telegram channel pairing or allowlist using the existing bot token and chat id.

## Phase 1.5: Dockerize gateway

- Image tag: pinned to `ghcr.io/openclaw/openclaw:2026.5.7-slim`, derived from latest stable release `v2026.5.7` published at `2026-05-07T20:57:43Z`. The GHCR package page lists `2026.5.7-slim` alongside `latest` for that release; using the slim tag keeps the image stable without tracking `latest`.
- Overlay image: `docker/openclaw-gateway/Dockerfile` starts from the pinned OpenClaw image and installs only `python3` plus `ca-certificates`. This is needed because the chosen MCP transport is stdio and the Jobhunter MCP bridge is a Python module.
- MCP transport: stdio with this repository mounted read-only at `/opt/jobhunter`. Config uses `command: "python3"`, `args: ["-m", "jobhunter.openclaw_mcp"]`, `cwd: "/opt/jobhunter"`, and `JOBHUNTER_SERVICE_URL=http://jobhunter-service:8765`. I chose stdio because OpenClaw documents stdio as the native local MCP server transport and our bridge already implements it; implementing a new Streamable HTTP MCP server would add protocol surface area without improving the trust boundary.
- Codex auth: host `~/.codex` exists and is mounted read-only at `/home/node/.codex`. No `openclaw migrate codex` command is baked into startup because the mounted Codex OAuth profile should be readable directly by the Dockerized runtime. If `openclaw doctor` later reports stale Codex model routes, run `./bin/openclaw migrate-codex` and then `./bin/openclaw doctor`.
- Sandbox mode: `agents.defaults.sandbox.mode` is `off`. This avoids mounting `/var/run/docker.sock` into the gateway. Protection comes from the gateway container boundary, read-only rootfs, narrow read-only repo/skills/Codex mounts, `cap_drop: ALL`, `no-new-privileges`, tool deny-lists, `exec.security: deny`, and bounded Jobhunter MCP tools.
- Docker onboarding: `./bin/openclaw onboard` runs OpenClaw's Docker manual flow with `node dist/index.js onboard --mode local --no-install-daemon`, then applies local gateway bind settings. The actual Telegram pairing and parity checks remain user-run acceptance steps.

## Phase 1.5b: OpenClaw inline keyboards

- Telegram channel capability: `./bin/openclaw onboard` now applies `channels.telegram.capabilities.inlineButtons=dm` and `channels.telegram.actions.sendMessage=true`. The printable config snippet includes the same settings for manual inspection or patching.
- Agent contract: the `jobhunter` skill instructs OpenClaw agents to send each digest item through the native `message` tool with four inline buttons: `Applied`, `Irrelevant`, `Snooze`, and `Cover`.
- Callback contract: OpenClaw injects unmatched `callback_data` as a synthetic user message. The skill treats `applied:<12_hex>`, `irrelevant:<12_hex>`, `snooze:<12_hex>`, and `cover:<12_hex>` as button callbacks and routes directly to Jobhunter MCP tools.
- Prefix resolution: `jobhunter-service` exposes `POST /jobs/resolve_prefix`, and MCP tools accept either `job_id` or `id_prefix`. Ambiguous or missing 12-character prefixes are rejected before any mutation.
- Audit behavior: inline job mutations write `agent_actions.kind='mark_job'` rows with the resolved full job id in `payload_json`, an applied status, and an applied timestamp.
- Original digest behavior: Phase 1.5b does not mutate the original digest card after a tap. Buttons remain visible; OpenClaw acknowledges the callback spinner and the agent should send only a short one-line confirmation or the cover draft.

## Phase 1.5c: Codex native MCP exposure fix

- Root cause: the Jobhunter MCP server was healthy, but Codex app-server did not use it because Codex's per-agent `config.toml` lacked MCP tool approval settings. In headless OpenClaw turns, Codex treated the MCP call as approval-gated and cancelled or stalled instead of invoking it.
- Rejected workaround: switching the whole agent to OpenClaw `codex-cli` mode made MCP discovery visible, but it is not the intended primary path for this setup. Upstream docs describe `codex-cli` as a CLI backend path; the fuller OpenClaw/Codex integration is the `codex` app-server harness.
- Final runtime choice: keep `agents.defaults.agentRuntime.id = "codex"` with `model.primary = "openai-codex/gpt-5.5"`.
- OpenClaw tool policy: `tools` must be top-level, not under `agents.defaults`, for OpenClaw 2026.5.7. Use `tools.profile = "messaging"` plus `tools.alsoAllow = ["web_search", "web_fetch"]`; avoid an explicit `tools.allow` list that names `bundle-mcp`, because this build logs it as an unknown allowlist entry.
- Native Codex shell policy: keep `plugins.entries.codex.config.appServer.approvalPolicy = "on-request"`. OpenClaw's `tools.exec.security = "deny"` does not remove Codex's native shell tool from the app-server harness; `on-request` prevents surprise shell execution while the approved Jobhunter MCP server remains callable headlessly.
- MCP tool descriptions: do not encode unconditional side effects into read tools. `jobhunter_get_more_jobs` must allow `mark_sent=false` diagnostics/analysis without requiring Telegram sends; otherwise Codex's safety monitor can cancel the call before the real service is reached.
- Codex MCP approval: `./bin/openclaw onboard` writes the following bounded approval into `/home/node/.openclaw/agents/main/agent/codex-home/config.toml`:

  ```toml
  [mcp_servers.jobhunter]
  default_tools_approval_mode = "approve"
  command = "python3"
  args = ["-m", "jobhunter.openclaw_mcp"]
  cwd = "/opt/jobhunter"

  [mcp_servers.jobhunter.env]
  JOBHUNTER_SERVICE_URL = "http://jobhunter-service:8765"
  ```

- Security note: approving this MCP server is narrower than relaxing shell execution. Jobhunter MCP tools are bounded Python service endpoints; shell/runtime/fs OpenClaw tools remain denied, native Codex shell is approval-gated, Codex app-server sandbox is forced to `read-only`, and the gateway still has no Docker socket.
- Verification: a fresh native Codex app-server session `phase15c-native-tools-check-approve` searched for `jobhunter_get_more_jobs`, called `mcp__jobhunter__.jobhunter_get_more_jobs`, and received 3 real job rows. After the shell/tool-description cleanup, session `phase15c-soft-contract-check` used only `jobhunter.jobhunter_get_more_jobs` in `toolSummary` and returned one real row with `mark_sent=false`.

## Phase 2: Retire legacy Telegram/worker/IPC path

- Python is now a headless domain service. OpenClaw owns Telegram, Codex sessions, inline buttons, and the user-facing turn loop.
- Removed tracked legacy runtime files: `openclaw/worker/`, `openclaw/prompts/`, `jobhunter/telegram.py`, `jobhunter/agent.py`, and their dedicated tests.
- Removed ignored local legacy state directories: `openclaw/workspace/` and `openclaw/codex-home/`. The Dockerized gateway uses the named `openclaw_home` volume and the read-only host `~/.codex` mount instead.
- Kept `openclaw-gateway` in `docker-compose.yml`. It is the real OpenClaw runtime from Phase 1.5, not the retired custom worker.
- Kept the three-part MCP registration in `./bin/openclaw onboard`: OpenClaw config patch, Codex `config.toml` server entry with `default_tools_approval_mode = "approve"`, and `codex mcp add jobhunter -- python3 -m jobhunter.openclaw_mcp`.
- Added `plugins/jobhunter-tools/` as an OpenClaw dynamic tool plugin. This is separate from top-level `mcp.servers`: top-level config plus `codex mcp add` makes Codex-native MCP work and emits `mcp_tool_call_*` logs, while the OpenClaw plugin makes Jobhunter actions appear as trajectory-visible `tool.call` events.
- Investigated a Codex bundle MCP plugin first and rejected it for Phase 2 acceptance: OpenClaw loads bundle MCP config for other runtime paths, but Codex app-server dynamic tools did not include bundle MCP tools in `session.started`, so no Jobhunter `tool.call` events appeared in trajectories.
- `tools.alsoAllow` now includes the narrow plugin id `jobhunter-tools`. Without that allowlist entry, the plugin loaded correctly but Codex sessions still started with only the default messaging/web/session tools.
- Verification sessions:
  - `phase2-openclaw-tool-diagnostic-3`: `session.started.toolCount=17`; trajectory includes `tool.call name=jobhunter_get_more_jobs` and successful `tool.result`.
  - Telegram session `8abb337f-8676-4f98-a6cf-f79565aedafc`, run `7e885563-aa10-4c50-ab81-a2fef158f08e`: current run starts with 17 tools, calls `jobhunter_get_more_jobs`, `jobhunter_collect_all_sources`, `jobhunter_get_more_jobs`, then five `message` calls with `presentation.blocks[].buttons`; no `bash` tool calls in that run.
  - `phase2-collect-soft-timeout`: `jobhunter_collect_all_sources` returns successful `status=running` instead of an OpenClaw `tool.timeout` while the background collection continues.
  - `phase2-usage-check-2` and `phase2-history-check`: trajectory-visible `jobhunter_usage` / `jobhunter_history` calls succeed.
- Codex 0.128.0 logs do not contain literal `mcp_tool_call_begin` / `mcp_tool_call_end` strings. Native Codex MCP evidence appears as `mcp__jobhunter__...` tool names with `mcp_tool=true` in `logs_2.sqlite`, plus `rmcp::service ... CallToolRequest ... name: "jobhunter_get_more_jobs"` rows. OpenClaw dynamic tool calls show `mcp_tool=false`, as expected, because they are OpenClaw plugin tools rather than native Codex MCP calls.
- `jobhunter-service` no longer publishes a host port. The OpenClaw gateway reaches it on the Compose network as `http://jobhunter-service:8765`.
- `./bin/jobhunter` remains for one release as a deprecated wrapper that delegates to `./bin/openclaw`.
