# OpenClaw Migration Discovery

Generated during Phase 0 of `MIGRATION.md`.

## Upstream Findings

Real OpenClaw is a local-first gateway with channels, sessions, skills, plugins, MCP, cron, sandboxing, and model/runtime routing. The docs confirm the broad migration direction, but not every detail in `MIGRATION.md` is exact.

## Skill Manifest Format

OpenClaw uses AgentSkills-compatible skill folders. A skill is a directory with `SKILL.md` and YAML frontmatter. The minimum supported format is:

```markdown
---
name: jobhunter
description: Operate the Jobhunter service for job search, ranking, source discovery, and approved actions.
---

Instructions for the agent go here. Use `{baseDir}` to reference files in this skill folder.
```

The parser supports single-line frontmatter keys. `metadata` must be a single-line JSON object when used.

## Tool Registration Model

Important correction: skills teach the agent how to use tools, but they do not register tools by themselves. OpenClaw tools come from built-ins, plugins, or configured MCP servers. Phase 1 used a stdio MCP bridge as a temporary compatibility path:

1. Keep Python domain logic in `jobhunter-service`.
2. Expose bounded actions over localhost HTTP.
3. Register OpenClaw-callable tools through a stdio MCP server.
4. Install `skills/jobhunter/SKILL.md` so the agent knows when and how to call those tools.

Phase 2 retired that bridge after plugin parity was proven. Current runtime uses `plugins/jobhunter-tools/` as the sole Jobhunter tool surface because it produces trajectory-visible `tool.call name=jobhunter_*` events.

## Telegram Channel Config Keys

OpenClaw Telegram config is under `channels.telegram`. Relevant keys:

```json5
{
  channels: {
    telegram: {
      enabled: true,
      botToken: "${TELEGRAM_BOT_TOKEN}",
      dmPolicy: "allowlist",
      allowFrom: ["tg:${TELEGRAM_ALLOWED_CHAT_ID}"]
    }
  }
}
```

`pairing` is the default safe DM policy. This repo's example uses `allowlist` so only the known chat id can reach the bot once the exact Telegram sender id format is confirmed by `openclaw doctor`.

## Cron Declaration Syntax

OpenClaw supports cron under the root `cron` config object. The exact job schema should be validated with `openclaw config schema` before enabling. The safe intended shape is:

```json5
{
  cron: {
    enabled: true,
    maxConcurrentRuns: 1,
    jobs: [
      {
        name: "jobs-collection",
        schedule: "0 */4 * * *",
        agentId: "jobs",
        message: "Run jobhunter collection, then summarize what changed."
      }
    ]
  }
}
```

## Sandbox Config Keys

OpenClaw docs confirm `agents.defaults.sandbox.mode: "non-main"` and Docker-backed sandboxing. Exact tool policy should be schema-validated before writing to `~/.openclaw/openclaw.json`.

```json5
{
  agents: {
    defaults: {
      sandbox: {
        mode: "non-main",
        scope: "agent"
      },
      tools: {
        profile: "messaging",
        deny: ["group:runtime", "group:fs", "group:automation"],
        allow: ["group:messaging", "bundle-mcp", "web_fetch", "web_search"]
      },
      skills: ["jobhunter"]
    }
  }
}
```

Community issue research shows sandbox/skill handling has had real edge cases around host skill paths and writable skill folders. Keep Jobhunter write authority in the Python service and prefer narrow plugin tools over broad filesystem or shell access.

## Codex Runtime Integration

OpenClaw distinguishes provider, model, and runtime. For the subscription-backed Codex path, current docs say `openai/*` agent model refs can run through the native Codex app-server runtime using Codex OAuth auth profiles. In other words, using `openai/gpt-5.5` in OpenClaw does not necessarily mean OpenAI API billing; with the bundled Codex plugin/auth route, it can use the ChatGPT/Codex subscription path. The exact local auth migration should be performed by `openclaw onboard`, `openclaw doctor --fix`, and, where relevant, `openclaw migrate codex`.

## Approval-Required Local Steps

These are intentionally not executed by Codex without user approval:

- `git checkout -b openclaw-migration`
- `npm install -g openclaw@latest`
- `openclaw onboard --install-daemon`
- Editing `~/.openclaw/openclaw.json`
- Running a live Telegram pairing flow
- Deleting the legacy custom worker and Telegram client before parity is proven
