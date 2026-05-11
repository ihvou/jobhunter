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
