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
            self.assertIn("data/email_samples", payload["available_files"])
            self.assertIn("jobhunter/database.py", payload["available_files"])
            self.assertIn("jobs", payload["db_tables"])
            self.assertIn("sources", payload["db_tables"])
            self.assertEqual(payload["counts"]["jobs_total"], 30)
            self.assertEqual(payload["counts"]["sources_total"], 1)

    def test_create_request_includes_recent_agent_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text((ROOT / "input" / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8")
            bot = JobHunter(config)
            first = bot.agent.create_request("show me jobs from harvey")
            response_path = config.workspace_dir / "agent" / ("response-%s.json" % first)
            response_path.write_text(
                json.dumps(
                    {
                        "user_intent_summary": "Harvey jobs",
                        "answer": "Found 3 Harvey jobs that matched your profile. " * 20,
                        "proposed_actions": [
                            {"kind": "data_answer", "summary": "Show rows", "payload": {"answer": "3 jobs"}},
                            {"kind": "rescore_jobs", "summary": "Refresh scores", "payload": {"window_hours": 24}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.update_agent_run(first, status="done", response_path=str(response_path), message="fallback")
            bot.database.record_agent_action(
                first,
                "rescore_jobs",
                "show me jobs from harvey",
                "Refresh scores",
                {"window_hours": 24},
                "applied",
                result_message="Rescored 12 job(s)",
            )

            second = bot.agent.create_request("how many did you find?")
            payload = json.loads((config.workspace_dir / "agent" / ("request-%s.json" % second)).read_text(encoding="utf-8"))

            self.assertEqual(len(payload["recent_agent_runs"]), 1)
            memory = payload["recent_agent_runs"][0]
            self.assertEqual(memory["session_id"], first)
            self.assertEqual(memory["user_text"], "show me jobs from harvey")
            self.assertLessEqual(len(memory["answer_excerpt"]), 300)
            self.assertEqual(memory["proposed_action_kinds"], ["data_answer", "rescore_jobs"])
            self.assertEqual(memory["applied_action_count"], 1)
            self.assertEqual(len(payload["recent_actions_summary"]), 1)
            self.assertEqual(payload["recent_actions_summary"][0]["kind"], "rescore_jobs")
            self.assertEqual(payload["recent_actions_summary"][0]["result_message_excerpt"], "Rescored 12 job(s)")

    def test_create_request_keeps_only_last_five_agent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text((ROOT / "input" / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8")
            bot = JobHunter(config)
            agent_dir = config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(10):
                session_id = "20260511000000%02d" % idx
                response_path = agent_dir / ("response-%s.json" % session_id)
                response_path.write_text(
                    json.dumps({"answer": "answer %s" % idx, "proposed_actions": [{"kind": "data_answer", "payload": {}}]}),
                    encoding="utf-8",
                )
                bot.database.create_agent_run(
                    session_id,
                    "request %s" % idx,
                    str(agent_dir / ("request-%s.json" % session_id)),
                    str(agent_dir / ("status-%s.json" % session_id)),
                )
                bot.database.update_agent_run(session_id, status="done", response_path=str(response_path), message="answer %s" % idx)

            current = bot.agent.create_request("what happened last?")
            payload = json.loads((agent_dir / ("request-%s.json" % current)).read_text(encoding="utf-8"))
            session_ids = [row["session_id"] for row in payload["recent_agent_runs"]]

            self.assertEqual(len(session_ids), 5)
            self.assertEqual(session_ids, ["2026051100000009", "2026051100000008", "2026051100000007", "2026051100000006", "2026051100000005"])
            self.assertNotIn("2026051100000004", session_ids)


if __name__ == "__main__":
    unittest.main()
