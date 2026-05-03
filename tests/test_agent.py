import json
import tempfile
import unittest
from pathlib import Path

from jobhunter.app import JobHunter
from jobhunter.models import Job, ScoreResult, SourceConfig
from test_app import config_for


ROOT = Path(__file__).resolve().parent.parent


class AgentCoordinatorTests(unittest.TestCase):
    def test_create_request_is_metadata_only_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text((ROOT / "input" / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8")
            bot = JobHunter(config)
            bot.database.upsert_sources([SourceConfig(id="s", name="S", type="rss", url="https://example.com/rss")])
            for idx in range(30):
                job_id, _ = bot.database.upsert_job(
                    Job(
                        source_id="s",
                        source_name="S",
                        external_id=str(idx),
                        url="https://example.com/%s" % idx,
                        title="AI Product Manager %s" % idx,
                        company="C",
                        description=("Build AI agent products. " * 80),
                    )
                )
                bot.database.save_score(job_id, ScoreResult(score=80, hard_reject=False))

            session_id = bot.agent.create_request("find better sources")
            payload = json.loads((config.workspace_dir / "agent" / ("request-%s.json" % session_id)).read_text(encoding="utf-8"))

            self.assertLess(len(json.dumps(payload)), 5000)
            self.assertNotIn("profile_md_full", payload)
            self.assertNotIn("recent_jobs_sample", payload)
            self.assertNotIn("sources_summary", payload)
            self.assertNotIn("recent_feedback_summary", payload)
            self.assertIn("input/profile.local.md", payload["available_files"])
            self.assertIn("jobhunter/database.py", payload["available_files"])
            self.assertIn("jobs", payload["db_tables"])
            self.assertIn("sources", payload["db_tables"])
            self.assertEqual(payload["counts"]["jobs_total"], 30)
            self.assertEqual(payload["counts"]["sources_total"], 1)


if __name__ == "__main__":
    unittest.main()
