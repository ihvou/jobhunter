# Jobhunter OpenClaw Skill

This skill teaches OpenClaw how to operate the local Jobhunter service. It assumes:

- `jobhunter-service` is running on the Compose network at `http://jobhunter-service:8765`.
- The OpenClaw agent has an MCP server named `jobhunter`.
- The skill directory is visible to OpenClaw at `/openclaw/skills`.

## MCP Server Config

Validate exact paths with `./bin/openclaw doctor` before editing live config.

```json5
{
  mcp: {
    servers: {
      jobhunter: {
        command: "python3",
        args: ["-m", "jobhunter.openclaw_mcp"],
        cwd: "/opt/jobhunter",
        env: {
          JOBHUNTER_SERVICE_URL: "http://jobhunter-service:8765"
        }
      }
    }
  },
  skills: {
    load: {
      extraDirs: ["/openclaw/skills"]
    }
  },
  agents: {
    defaults: {
      skills: ["jobhunter"]
    }
  }
}
```

## Quick Smoke

```bash
./bin/openclaw onboard
./bin/openclaw start
./bin/openclaw doctor
./bin/openclaw config
```

Then ask OpenClaw:

```text
Get more jobs from Jobhunter.
```

Expected behavior: OpenClaw calls `jobhunter_get_more_jobs`, summarizes the ranked jobs, and does not edit files directly.

For changes, OpenClaw should call `jobhunter_propose_actions` first, show the returned action ids to you, and only call `jobhunter_apply_action` after you approve the exact id.
