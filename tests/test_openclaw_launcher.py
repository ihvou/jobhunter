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
        for command in ("start", "stop", "restart", "logs", "status", "shell", "onboard", "doctor", "migrate-codex", "config"):
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
        self.assertIn('agentRuntime', result.stdout)
        self.assertIn('id: "codex"', result.stdout)
        self.assertIn('primary: "openai-codex/gpt-5.5"', result.stdout)
        self.assertIn('alsoAllow: ["web_search", "web_fetch", "jobhunter-tools", "firecrawl", "exa"]', result.stdout)
        self.assertIn('allow: ["codex", "telegram", "jobhunter-tools", "firecrawl", "exa", "memory-core", "openai"]', result.stdout)
        self.assertIn('approvalPolicy: "on-request"', result.stdout)
        self.assertIn('sandbox: "read-only"', result.stdout)
        self.assertIn("/opt/jobhunter/plugins/jobhunter-tools", result.stdout)
        self.assertIn('"jobhunter-tools"', result.stdout)

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
        self.assertIn("mcp remove jobhunter", result.stdout)
        self.assertNotIn("jobhunter.openclaw_mcp", result.stdout)
        self.assertNotIn("default_tools_approval_mode", result.stdout)


if __name__ == "__main__":
    unittest.main()
