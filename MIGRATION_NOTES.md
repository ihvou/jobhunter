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
