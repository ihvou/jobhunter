# Migration Notes

## Decisions

- Implement Phase 1 as a compatibility bridge first. Full Phase 2 deletion is deferred until real OpenClaw is installed, configured, and proven against Telegram.
- Use a stdlib HTTP `jobhunter-service` plus an OpenClaw dynamic tool plugin instead of speculative declarative YAML tools. Upstream OpenClaw docs describe skills as instructions, not executable tool registrations.
- Keep the legacy Telegram bot and custom worker in place during this commit. They remain the rollback path until OpenClaw parity is verified.
- Publish the Python service on `127.0.0.1:8765` from Docker. This is loopback-only, not externally reachable, and lets a host-native OpenClaw gateway call the service on macOS.
- Keep the service's `config/` and `input/` mounts writable. OpenClaw itself does not receive broad filesystem write access; approved writes go through bounded `jobhunter-service` action endpoints that archive and audit changes.

## Spec Discrepancies

- `docs/automation/gmail-pubsub.md` currently resolves as an empty/placeholder doc in the upstream repo. Gmail Pub/Sub migration should not be implemented until the concrete schema and hook flow are verified.
- The proposed `skills/jobhunter/tools/*.yml` HTTP declaration format is not validated by upstream docs. The implemented path uses `plugins/jobhunter-tools/`.
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
- The `jobhunter-tools` OpenClaw plugin loaded from this repo.
- Telegram channel pairing or allowlist using the existing bot token and chat id.

## Phase 1.5: Dockerize gateway

- Image tag: pinned to `ghcr.io/openclaw/openclaw:2026.5.7-slim`, derived from latest stable release `v2026.5.7` published at `2026-05-07T20:57:43Z`. The GHCR package page lists `2026.5.7-slim` alongside `latest` for that release; using the slim tag keeps the image stable without tracking `latest`.
- Overlay image: `docker/openclaw-gateway/Dockerfile` starts from the pinned OpenClaw image and installs only `python3` plus `ca-certificates` for service/debug scripts. Jobhunter tools themselves are now exposed through the OpenClaw plugin.
- Tool transport: the `jobhunter-tools` plugin calls `jobhunter-service` over the Compose network at `http://jobhunter-service:8765`.
- Codex auth: host `~/.codex` exists and is mounted read-only at `/home/node/.codex`. No `openclaw migrate codex` command is baked into startup because the mounted Codex OAuth profile should be readable directly by the Dockerized runtime. If `openclaw doctor` later reports stale Codex model routes, run `./bin/openclaw migrate-codex` and then `./bin/openclaw doctor`.
- Sandbox mode: `agents.defaults.sandbox.mode` is `off`. This avoids mounting `/var/run/docker.sock` into the gateway. Protection comes from the gateway container boundary, read-only rootfs, narrow read-only repo/skills/Codex mounts, `cap_drop: ALL`, `no-new-privileges`, tool deny-lists, `exec.security: deny`, and bounded Jobhunter plugin tools.
- Docker onboarding: `./bin/openclaw onboard` runs OpenClaw's Docker manual flow with `node dist/index.js onboard --mode local --no-install-daemon`, then applies local gateway bind settings. The actual Telegram pairing and parity checks remain user-run acceptance steps.

## Phase 1.5b: OpenClaw inline keyboards

- Telegram channel capability: `./bin/openclaw onboard` now applies `channels.telegram.capabilities.inlineButtons=dm` and `channels.telegram.actions.sendMessage=true`. The printable config snippet includes the same settings for manual inspection or patching.
- Agent contract: the `jobhunter` skill instructs OpenClaw agents to send each digest item through the native `message` tool with four inline buttons: `Applied`, `Irrelevant`, `Snooze`, and `Cover`.
- Callback contract: OpenClaw injects unmatched `callback_data` as a synthetic user message. The skill treats `applied:<12_hex>`, `irrelevant:<12_hex>`, `snooze:<12_hex>`, and `cover:<12_hex>` as button callbacks and routes directly to Jobhunter plugin tools.
- Prefix resolution: `jobhunter-service` exposes `POST /jobs/resolve_prefix`, and plugin tools accept either `job_id` or `id_prefix`. Ambiguous or missing 12-character prefixes are rejected before any mutation.
- Audit behavior: inline job mutations write `agent_actions.kind='mark_job'` rows with the resolved full job id in `payload_json`, an applied status, and an applied timestamp.
- Original digest behavior: Phase 1.5b does not mutate the original digest card after a tap. Buttons remain visible; OpenClaw acknowledges the callback spinner and the agent should send only a short one-line confirmation or the cover draft.

## Phase 1.5c: Codex native MCP exposure fix

- Historical result: Codex-native MCP was made to work during 1.5c, but Phase 2 acceptance proved the OpenClaw dynamic plugin is the runtime surface the Telegram agent actually uses. The useful signal is bare `tool.call name=jobhunter_*` in OpenClaw trajectories.
- Retired in follow-up: `mcp.servers.jobhunter`, Codex `config.toml` `[mcp_servers.jobhunter]`, and `codex mcp add jobhunter` were removed to avoid exposing the same 10 tools twice.
- Current runtime choice: keep `agents.defaults.agentRuntime.id = "codex"` with `model.primary = "openai-codex/gpt-5.5"`, and expose Jobhunter solely through `plugins/jobhunter-tools/`.
- OpenClaw tool policy: `tools` must be top-level, not under `agents.defaults`, for OpenClaw 2026.5.7. Use `tools.profile = "messaging"` plus `tools.alsoAllow = ["web_search", "web_fetch", "jobhunter-tools"]`; avoid broad `group:plugins`.
- Native Codex shell policy: keep `plugins.entries.codex.config.appServer.approvalPolicy = "on-request"`. OpenClaw's `tools.exec.security = "deny"` does not remove Codex's native shell tool from the app-server harness; `on-request` prevents surprise shell execution.
- Plugin tool descriptions are authoritative for rendering, callback data, and staleness behavior. `SKILL.md` remains duplicate guidance, not the source of truth.

## Phase 2: Retire legacy Telegram/worker/IPC path

- Python is now a headless domain service. OpenClaw owns Telegram, Codex sessions, inline buttons, and the user-facing turn loop.
- Removed tracked legacy runtime files: `openclaw/worker/`, `openclaw/prompts/`, `jobhunter/telegram.py`, `jobhunter/agent.py`, and their dedicated tests.
- Removed ignored local legacy state directories: `openclaw/workspace/` and `openclaw/codex-home/`. The Dockerized gateway uses the named `openclaw_home` volume and the read-only host `~/.codex` mount instead.
- Kept `openclaw-gateway` in `docker-compose.yml`. It is the real OpenClaw runtime from Phase 1.5, not the retired custom worker.
- Added `plugins/jobhunter-tools/` as the sole Jobhunter tool surface. It makes Jobhunter actions appear as trajectory-visible `tool.call name=jobhunter_*` events.
- Investigated a Codex bundle MCP plugin first and rejected it for Phase 2 acceptance: OpenClaw loads bundle MCP config for other runtime paths, but Codex app-server dynamic tools did not include bundle MCP tools in `session.started`, so no Jobhunter `tool.call` events appeared in trajectories.
- `tools.alsoAllow` now includes the narrow plugin id `jobhunter-tools`. Without that allowlist entry, the plugin loaded correctly but Codex sessions still started with only the default messaging/web/session tools.
- Verification sessions:
  - `phase2-openclaw-tool-diagnostic-3`: `session.started.toolCount=17`; trajectory includes `tool.call name=jobhunter_get_more_jobs` and successful `tool.result`.
  - Telegram session `8abb337f-8676-4f98-a6cf-f79565aedafc`, run `7e885563-aa10-4c50-ab81-a2fef158f08e`: current run starts with 17 tools, calls `jobhunter_get_more_jobs`, `jobhunter_collect_all_sources`, `jobhunter_get_more_jobs`, then five `message` calls with `presentation.blocks[].buttons`; no `bash` tool calls in that run.
  - `phase2-collect-soft-timeout`: `jobhunter_collect_all_sources` returns successful `status=running` instead of an OpenClaw `tool.timeout` while the background collection continues.
  - `phase2-usage-check-2` and `phase2-history-check`: trajectory-visible `jobhunter_usage` / `jobhunter_history` calls succeed.
- Phase 2 acceptance evidence: `phase2-openclaw-tool-diagnostic-3` started with `toolCount=17` and called `jobhunter_get_more_jobs`; Telegram session `8abb337f-8676-4f98-a6cf-f79565aedafc` called `jobhunter_get_more_jobs`, `jobhunter_collect_all_sources`, then emitted five `message` calls with `presentation.blocks[].buttons`.
- `jobhunter-service` no longer publishes a host port. The OpenClaw gateway reaches it on the Compose network as `http://jobhunter-service:8765`.
- `./bin/jobhunter` remains for one release as a deprecated wrapper that delegates to `./bin/openclaw`.

## Phase 3a: Firecrawl/Exa and email-alert cleanup

- Firecrawl and Exa are bundled in the pinned gateway image as `@openclaw/firecrawl-plugin@2026.5.7` and `@openclaw/exa-plugin@2026.5.7`; no community package was installed. The OpenClaw registry search returned an unrelated community wrapper (`web-search-plus-plugin-v2@2.5.3`), so the safer path is enabling the bundled official plugins pinned by `ghcr.io/openclaw/openclaw:2026.5.7-slim`.
- `FIRECRAWL_API_KEY` and `EXA_API_KEY` are passed into `openclaw-gateway` from `.env`. With keys missing, the bundled plugins can still be configured, but live Firecrawl/Exa calls are expected to fail with a missing API-key/auth error. After the keys were provided, the gateway was recreated and both plugins loaded successfully.
- Firecrawl is enabled for `webFetch` with conservative settings: `onlyMainContent=true`, `maxAgeMs=86400000`, `timeoutSeconds=30`. Exa is enabled as a web-search provider through its bundled plugin. Both are explicitly allowed through top-level `tools.alsoAllow`.
- `plugins.allow` is now explicit: `codex`, `telegram`, `jobhunter-tools`, `firecrawl`, `exa`, `memory-core`, and `openai`. The Phase 3a spec listed six plugins, but live OpenClaw retained `openai` because the Codex/model stack uses the bundled OpenAI provider path. Keeping it explicit prevents the allowlist from drifting while avoiding model-routing surprises.
- Exa is a web-search provider in OpenClaw 2026.5.7, not a direct tool provider; it may not appear as a separate `tool.call name=exa_*`. Firecrawl does expose direct `firecrawl_search` and `firecrawl_scrape` tools, which are the trajectory signal for DOU acceptance.
- Email alert parsing now drops wrapper rows before insertion when titles are exactly `Read more`, contain `new jobs match`, contain `Top job picks`, or are shorter than 8 characters.
- Firecrawl/Exa smoke evidence:
  - `phase3a-firecrawl-dou-smoke`: `session.started.toolCount=19`; trajectory includes `tool.call name=firecrawl_scrape` and successful `tool.result`; DOU Product Manager page returned through Firecrawl.
  - `phase3a-exa-smoke`: `session.started.toolCount=19`; trajectory includes `tool.call name=web_search` and successful `tool.result`; Exa is provider-backed, so no separate `exa_*` tool call is expected in this OpenClaw build.
- DOU end-to-end acceptance did not fully pass under Phase 3a constraints:
  - Telegram-delivered session `phase3a-dou-acceptance` proposed action `43` for `https://jobs.dou.ua/vacancies/?category=Product%20Manager&from=maybe`, then used `firecrawl_scrape` successfully before approval.
  - `approve 43` called `jobhunter_apply_action`, but the Python service rejected the source with `SourceError: HEAD probe failed`.
  - Direct service probes from `jobhunter-service` return `403 Forbidden` for both the DOU page URL and `https://jobs.dou.ua/vacancies/feeds/?category=Product%20Manager`; current SQL count is `0` DOU jobs.
  - This is not a Firecrawl failure. It is the known gap that Phase 3b names as "firecrawl-backed source validation"; Phase 3a explicitly says not to hack or soften `validate_source_row`.

One-shot operator cleanup SQL for existing email-alert noise rows after deploy:

```sql
UPDATE jobs SET status='irrelevant'
WHERE source_id='email-job-alerts'
  AND (title='Read more' OR title LIKE '%new jobs match%'
       OR title LIKE '%Top job picks%' OR length(title) < 8)
  AND status='new';
```

Run this manually through a controlled SQL path after reviewing the impact; the agent should not run it autonomously.

## Phase 3b: Firecrawl-backed community sources and email trigger bridge

- `jobhunter-service` now receives `FIRECRAWL_API_KEY` too, not only `openclaw-gateway`.
- Community source approval keeps the direct HEAD/GET probe first. If that fails and the proposed source type is `community`, the service tries a bounded Firecrawl scrape before rejecting the proposal. This keeps RSS/API/ATS validation unchanged while allowing geo/WAF-blocked job pages to enter as `status=test` sources when Firecrawl can read them.
- Community collection now uses the same fallback: direct fetch first, Firecrawl scrape second. Firecrawl markdown links are parsed alongside HTML links, so DOU-style pages can produce job rows after approval.
- OpenClaw receives a new `jobhunter_process_email` plugin tool. It accepts one already-parsed email (`sender`, `subject`, `body`, optional `message_id`/`date`) and sends it to `jobhunter-service` for the existing email parser, noise filter, scoring, and capped L2 relevance.
- The service endpoint behind that tool is `POST /email/process`. It does not read Gmail directly, send email, or open OAuth scopes; it only ingests message content supplied by an approved OpenClaw/Gmail hook or future email skill.
- The bundled OpenClaw image does not include an `agenticmail` plugin/skill. The practical Phase 3b bridge is therefore OpenClaw Gmail Pub/Sub/hooks -> `jobhunter_process_email` -> existing parser DSL. If a real agenticmail package is later selected, it should call the same tool rather than bypassing the service.
- GCP project, Gmail OAuth, Tailscale/public hook URL, and `openclaw webhooks gmail setup --account <account>` remain operator approval steps. They should be done only after verifying the local `jobhunter_process_email` trajectory works.
- Phase 3b verification:
  - `phase3b-dou-acceptance`: `session.started.toolCount=20`; trajectory includes `firecrawl_scrape`, `jobhunter_propose_actions`, `jobhunter_apply_action`, `jobhunter_collect_all_sources`, and `jobhunter_query_sql`.
  - Action `62` added `dou-product-manager` as a `community` `status=test` source. Service logs show `source_candidate_firecrawl_probe reachable=true` and `community_source_firecrawl_fetch_succeeded` for the DOU URL.
  - Collection inserted DOU rows: SQL count was `26` immediately after first collection; after parser-filter cleanup, `18` DOU rows remain `new` and `8` DOU navigation/filter rows were archived via audited action `63`.
  - `phase3b-email-process-smoke`: trajectory includes `jobhunter_process_email` with `jobs_found=0`, `inserted=0` for a wrapper-only email body, proving the new bridge tool is visible and the noise filter is applied.

## Phase 4: Recurring jobs + Leadhunter

- OpenClaw config enables cron, and onboarding attempts to register CLI-backed cron jobs for collection every 4 hours, daily rescore, and monthly source discovery. The scheduled prompts call `jobhunter_collect_all_sources`, `jobhunter_rescore_recent_jobs`, or `jobhunter_propose_actions`; they do not require shell access. Live validation on OpenClaw 2026.5.7 rejected declarative `cron.jobs`, so `./bin/openclaw cron-install` is the supported path for this pinned runtime.
- Live note: after accepting the config and restarting the gateway, `cron-install` can still be blocked by OpenClaw's gateway scope approval (`scope upgrade pending approval`). This is a real OpenClaw approval gate, not a Jobhunter failure; approve the pending scope in the OpenClaw UI and rerun `./bin/openclaw cron-install`.
- The plugin remains the single tool surface. It now includes five `leadhunter_*` tools plus `jobhunter_rescore_recent_jobs`; there is still no Codex-native MCP registration.
- SQLite schema v11 adds `leads`, `lead_sources`, `lead_feedback`, and `lead_drafts`. Lead URLs go through the same safe URL validator used for public source work.
- `input/icp.local.md` is private and gitignored. It is copied from `input/icp.example.md` on first init and used for pitch drafting.
- Leadhunter is intentionally human-in-the-loop: research can use OpenClaw web/search tools, but candidates are saved only after approval and pitch tools only draft copy-paste text.
