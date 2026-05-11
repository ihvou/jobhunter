import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "openclaw"


class OpenClawLauncherTests(unittest.TestCase):
    def run_launcher(self, *args):
        return subprocess.run([str(LAUNCHER), *args], cwd=ROOT, text=True, capture_output=True)

    def test_help_lists_bridge_commands(self):
        result = self.run_launcher("help")
        self.assertEqual(result.returncode, 0)
        for command in ("start", "stop", "restart", "logs", "status", "shell", "config"):
            self.assertIn(command, result.stdout)

    def test_config_prints_mcp_and_skill_paths(self):
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0)
        self.assertIn("jobhunter.openclaw_mcp", result.stdout)
        self.assertIn(str(ROOT / "skills"), result.stdout)


if __name__ == "__main__":
    unittest.main()
