import os
import tempfile
import unittest
from pathlib import Path

from jobbot.config import load_app_config, parse_optional_int, parse_profile_description


class ConfigTests(unittest.TestCase):
    def test_profile_description_extracts_product_titles(self):
        parsed = parse_profile_description("Product manager. Head of product. Build MVPs with Codex and AI automation.")
        self.assertIn("product manager", parsed["target_titles"])
        self.assertIn("head of product", parsed["target_titles"])
        self.assertIn("codex", parsed["positive_keywords"])

    def test_allowed_chat_id_is_stripped_and_coerced(self):
        self.assertEqual(parse_optional_int(" 123 "), 123)

    def test_env_overrides_json_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            settings = config_dir / "jobbot.json"
            settings.write_text('{"digest_max_jobs": 10}', encoding="utf-8")
            old_env = dict(os.environ)
            try:
                os.environ["JOBBOT_CONFIG_DIR"] = str(config_dir)
                os.environ["JOBBOT_SETTINGS_PATH"] = str(settings)
                os.environ["JOBBOT_DIGEST_MAX_JOBS"] = "5"
                config = load_app_config()
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(config.digest_max_jobs, 5)


if __name__ == "__main__":
    unittest.main()
