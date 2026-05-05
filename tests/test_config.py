import os
import tempfile
import unittest
from pathlib import Path

from jobhunter.config import ConfigError, ensure_profile_file, load_app_config, load_sources, parse_optional_int, parse_profile_description
from test_app import config_for

ROOT = Path(__file__).resolve().parent.parent


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
            settings = config_dir / "jobhunter.json"
            settings.write_text('{"digest_max_jobs": 10}', encoding="utf-8")
            old_env = dict(os.environ)
            try:
                os.environ["JOBHUNTER_CONFIG_DIR"] = str(config_dir)
                os.environ["JOBHUNTER_SETTINGS_PATH"] = str(settings)
                os.environ["JOBHUNTER_DIGEST_MAX_JOBS"] = "5"
                config = load_app_config()
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(config.digest_max_jobs, 5)

    def test_robots_txt_respect_defaults_to_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_env = dict(os.environ)
            try:
                os.environ["JOBHUNTER_DATA_DIR"] = str(Path(tmp) / "data")
                os.environ["JOBHUNTER_CONFIG_DIR"] = str(Path(tmp) / "config")
                os.environ["JOBHUNTER_INPUT_DIR"] = str(Path(tmp) / "input")
                os.environ.pop("JOBHUNTER_ROBOTS_TXT_RESPECT", None)
                config = load_app_config()
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(config.robots_txt_respect, "ignore")

    def test_missing_local_profile_and_cv_are_copied_from_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            (config.input_dir / "profile.example.md").write_text("# About me\nExample\n\n# Directives\n", encoding="utf-8")
            (config.input_dir / "cv.example.md").write_text("Example CV", encoding="utf-8")

            ensure_profile_file(config)

            self.assertEqual(config.profile_path.read_text(encoding="utf-8"), "# About me\nExample\n\n# Directives\n")
            self.assertEqual(config.cv_path.read_text(encoding="utf-8"), "Example CV")

    def test_invalid_source_type_fails_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text('[{"id":"bad","name":"Bad","type":"html","url":"https://example.com/jobs"}]', encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "invalid type 'html'"):
                load_sources(path)

    def test_baseline_sources_are_low_risk_for_trust_mode(self):
        sources = load_sources(ROOT / "config" / "sources.example.json")

        active_public = [source for source in sources if source.status == "active" and source.type != "imap"]
        self.assertTrue(active_public)
        self.assertTrue(all(source.risk_level == "low" for source in active_public))


if __name__ == "__main__":
    unittest.main()
