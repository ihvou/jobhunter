import json
import tempfile
import unittest
from pathlib import Path

from jobhunter.app import JobHunter
from jobhunter.models import Job, ScoreResult, SourceConfig
from test_app import config_for


ROOT = Path(__file__).resolve().parent.parent


class AgentCoordinatorTests(unittest.TestCase):
    def test_create_request_keeps_agent_context_under_budget(self):
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

            self.assertLess(len(json.dumps(payload)), 30000)
            self.assertEqual(len(payload["recent_jobs_sample"]), 15)
            self.assertTrue(all(len(job["description_excerpt"]) <= 250 for job in payload["recent_jobs_sample"]))


if __name__ == "__main__":
    unittest.main()
