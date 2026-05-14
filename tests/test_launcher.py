import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "jobhunter"


class DeprecatedLauncherTests(unittest.TestCase):
    def run_launcher(self, *args, env=None):
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run([str(LAUNCHER), *args], cwd=ROOT, text=True, capture_output=True, env=merged_env)

    def test_wrapper_points_to_openclaw(self):
        result = self.run_launcher("help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("deprecated", result.stderr)
        self.assertIn("Usage: ./bin/openclaw", result.stdout)

    def test_status_uses_openclaw_status_shape(self):
        result = self.run_launcher("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("jobhunter-service:", result.stdout)
        self.assertIn("openclaw-gateway:", result.stdout)


if __name__ == "__main__":
    unittest.main()
