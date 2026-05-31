import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "openclaw"


class OpenClawLauncherTests(unittest.TestCase):
    def run_launcher(self, *args, env=None):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run([str(LAUNCHER), *args], cwd=ROOT, text=True, capture_output=True, env=merged_env)

    def test_help_lists_bridge_commands(self):
        result = self.run_launcher("help")
        self.assertEqual(result.returncode, 0)
        for command in (
            "start",
            "stop",
            "restart",
            "logs",
            "status",
            "shell",
            "onboard",
            "cron-install",
            "cron-list",
            "doctor",
            "migrate-codex",
            "config",
            "ensure-engineer-workspace",
            "engineer-codex-status",
            "engineer-codex-login",
        ):
            self.assertIn(command, result.stdout)

    def test_config_prints_plugin_and_skill_paths(self):
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("jobhunter.openclaw_mcp", result.stdout)
        self.assertNotIn("mcp_servers.jobhunter", result.stdout)
        self.assertIn("/opt/jobhunter", result.stdout)
        self.assertIn("/openclaw/skills", result.stdout)
        self.assertIn('mode: "off"', result.stdout)
        self.assertIn('inlineButtons: "dm"', result.stdout)
        self.assertIn("sendMessage: true", result.stdout)
        self.assertIn("cron", result.stdout)
        self.assertIn('agentRuntime', result.stdout)
        self.assertIn('id: "codex"', result.stdout)
        self.assertIn('id: "leads"', result.stdout)
        self.assertIn('skills: ["leadhunter"]', result.stdout)
        self.assertIn('primary: "openai-codex/gpt-5.5"', result.stdout)
        self.assertIn('"codex-cli/gpt-5.5": {}', result.stdout)
        self.assertIn('cliBackends', result.stdout)
        self.assertIn('CODEX_HOME: "/home/node/.openclaw/agents/engineer/agent/codex-home"', result.stdout)
        # Engineer uses --dangerously-bypass-approvals-and-sandbox instead of --sandbox
        # workspace-write because Codex's bwrap-based sandbox can't create namespaces
        # inside our cap_drop:ALL container. The Docker container IS the sandbox.
        self.assertIn('"--dangerously-bypass-approvals-and-sandbox",', result.stdout)
        self.assertNotIn('"--sandbox",', result.stdout)
        self.assertIn('"--cd",', result.stdout)
        self.assertIn('"/workspace",', result.stdout)
        self.assertIn('alsoAllow: ["web_search", "web_fetch", "jobhunter-tools", "firecrawl", "exa"]', result.stdout)
        self.assertIn('allow: ["codex", "telegram", "jobhunter-tools", "firecrawl", "exa", "memory-core", "openai"]', result.stdout)
        self.assertIn('approvalPolicy: "on-request"', result.stdout)
        self.assertIn('sandbox: "read-only"', result.stdout)
        self.assertIn("/opt/jobhunter/plugins/jobhunter-tools", result.stdout)
        self.assertIn('"jobhunter-tools"', result.stdout)
        self.assertIn('id: "engineer"', result.stdout)
        self.assertIn('agentRuntime: { id: "codex-cli" }', result.stdout)
        self.assertIn('model: { primary: "codex-cli/gpt-5.5" }', result.stdout)
        self.assertIn('agentDir: "/home/node/.openclaw/agents/engineer/agent"', result.stdout)
        self.assertIn('workspace: "/workspace"', result.stdout)
        self.assertIn('security: "allowlist"', result.stdout)
        self.assertIn('safeBins: ["git", "gh", "python3", "node", "npm"]', result.stdout)

    def test_onboard_dry_run_uses_docker_gateway(self):
        result = self.run_launcher("onboard", env={"OPENCLAW_DRY_RUN": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("openclaw-gateway dist/index.js onboard --mode local --no-install-daemon", result.stdout)
        self.assertIn("exec -T openclaw-gateway node /app/dist/index.js config set --batch-json", result.stdout)
        self.assertIn("exec -T openclaw-gateway node /app/dist/index.js config patch --stdin", result.stdout)
        self.assertIn("inlineButtons", result.stdout)
        self.assertIn("sendMessage", result.stdout)
        self.assertIn("jobhunter-tools", result.stdout)
        self.assertIn("firecrawl", result.stdout)
        self.assertIn("exa", result.stdout)
        self.assertIn("codex-cli", result.stdout)
        # Engineer no longer uses --sandbox workspace-write at the CLI flag level
        # (bwrap fails inside our cap_drop:ALL container) — Docker container is
        # the sandbox. Use --dangerously-bypass-approvals-and-sandbox instead.
        self.assertIn("dangerously-bypass-approvals-and-sandbox", result.stdout)
        self.assertIn("/workspace", result.stdout)
        self.assertIn("/home/node/.openclaw/agents/engineer/agent/codex-home", result.stdout)
        self.assertIn("jobs-collection", result.stdout)
        self.assertIn("jobhunter_rescore_recent_jobs", result.stdout)
        self.assertIn("leadhunter", result.stdout)
        self.assertIn("mcp remove jobhunter", result.stdout)
        self.assertIn("ensure-engineer-workspace-script", result.stdout)
        self.assertNotIn("jobhunter.openclaw_mcp", result.stdout)
        self.assertNotIn("default_tools_approval_mode", result.stdout)

    def run_openclaw_dry_run(self, *args):
        env = os.environ.copy()
        env["OPENCLAW_DRY_RUN"] = "1"
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    def test_cron_install_declares_phase7_agents(self):
        out = self.run_openclaw_dry_run("cron-install")
        self.assertIn("--name jobs-collection", out)
        self.assertIn("--agent collector", out)
        self.assertIn("--name researcher-nightly", out)
        self.assertIn("--agent researcher", out)
        self.assertIn("--name qa-nightly", out)
        self.assertIn("--agent qa", out)
        readable_out = out.replace("\\ ", " ")
        self.assertIn("DB Anti-Pattern Checklist", readable_out)
        self.assertIn("stuck_picked_task", readable_out)
        self.assertIn("repeated_source_failures", readable_out)
        self.assertIn("placeholder_titles", readable_out)
        self.assertIn("stale_unparsed_email_alerts", readable_out)
        self.assertIn("failure_reports", readable_out)
        self.assertIn("digest_volume_drop", readable_out)
        self.assertIn("--name engineer-nightly", out)
        self.assertIn("--agent engineer", out)
        self.assertIn("--name pm-stakeholder", out)
        self.assertIn("--agent pm", out)
        # Pre-Phase-7 crons must not be ADDED (--name with cron add). They MAY
        # appear under `cron rm` because install_cron_jobs explicitly removes
        # them on every install to clean up upgrading users — that's expected.
        self.assertNotIn("--name jobs-discovery-monthly", out)
        self.assertNotIn("--name email-audit-nightly", out)
        self.assertNotIn("--name jobs-rescore-on-feedback-change", out)
        # And the explicit cleanup of those three stale crons IS expected.
        self.assertIn("cron rm jobs-rescore-on-feedback-change", out)
        self.assertIn("cron rm email-audit-nightly", out)
        self.assertIn("cron rm jobs-discovery-monthly", out)

    def test_engineer_workspace_dry_run_is_available(self):
        out = self.run_openclaw_dry_run("ensure-engineer-workspace")
        self.assertIn("ensure-engineer-workspace-script", out)

    def test_engineer_codex_auth_dry_run_uses_private_codex_home(self):
        status_out = self.run_openclaw_dry_run("engineer-codex-status")
        self.assertIn("CODEX_HOME=/home/node/.openclaw/agents/engineer/agent/codex-home", status_out)
        self.assertIn("login status", status_out)

        login_out = self.run_openclaw_dry_run("engineer-codex-login", "--reset")
        self.assertIn("auth.json.dead.", login_out)
        self.assertIn("login --device-auth", login_out)
        self.assertIn("It does not touch main OpenClaw app-server auth or host ~/.codex.", login_out)
        self.assertNotIn("--with-api-key", login_out)


if __name__ == "__main__":
    unittest.main()
