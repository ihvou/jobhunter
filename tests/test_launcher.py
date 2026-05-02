import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "jobhunter"


class LauncherTests(unittest.TestCase):
    def run_launcher(self, *args, input_text=None, env=None):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            env=merged_env,
        )

    def test_help_lists_common_commands(self):
        result = self.run_launcher("help")
        self.assertEqual(result.returncode, 0)
        for command in ("start", "stop", "restart", "logs", "status", "reset", "shell", "login"):
            self.assertIn(command, result.stdout)

    def test_start_refuses_missing_required_env(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("TELEGRAM_BOT_TOKEN=\n")
            env_path = handle.name
        try:
            result = self.run_launcher("start", env={"JOBHUNTER_ENV_FILE": env_path})
        finally:
            Path(env_path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing TELEGRAM_BOT_TOKEN in .env", result.stderr)
        self.assertIn("Missing TELEGRAM_ALLOWED_CHAT_ID in .env", result.stderr)

    def test_reset_cancels_without_confirmation(self):
        result = self.run_launcher("reset", input_text="no\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reset cancelled", result.stderr)

    def test_status_is_single_line(self):
        result = self.run_launcher("status")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIn("jobhunter:", result.stdout)
        self.assertIn("worker:", result.stdout)


if __name__ == "__main__":
    unittest.main()
