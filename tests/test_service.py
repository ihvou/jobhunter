import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobhunter.app import JobHunter
from jobhunter.models import Job, ScoreResult, SourceConfig
from jobhunter.service import JobHunterService, ServiceError
from test_app import config_for


ROOT = Path(__file__).resolve().parent.parent


class ServiceTests(unittest.TestCase):
    def seeded_bot(self, tmp):
        config = config_for(tmp)
        config.profile_path.write_text((ROOT / "input" / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8")
        bot = JobHunter(config)
        bot.initialize()
        bot.database.upsert_sources([SourceConfig(id="s", name="Source", type="rss", url="https://example.com/rss")])
        job_id, _ = bot.database.upsert_job(
            Job(
                source_id="s",
                source_name="Source",
                external_id="1",
                url="https://example.com/job",
                title="AI Product Manager",
                company="ExampleCo",
                description="Build AI agent workflows with product teams.",
            )
        )
        bot.database.save_score(job_id, ScoreResult(score=80, hard_reject=False, reasons=["AI product"], fired_rules=["title"]))
        return bot, job_id

    def test_digest_and_job_actions_are_exposed_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            digest = service.digest(limit=1)
            self.assertEqual(digest["count"], 1)
            self.assertEqual(digest["jobs"][0]["id"], job_id)
            self.assertEqual(digest["jobs"][0]["title"], "AI Product Manager")

            applied = service.mark_applied(job_id)
            self.assertTrue(applied["ok"])
            self.assertEqual(bot.database.get_job(job_id)["status"], "applied")
            action = bot.database.recent_agent_actions(1)[0]
            self.assertEqual(action["kind"], "mark_job")
            self.assertEqual(action["status"], "applied")
            payload = json.loads(action["payload_json"])
            self.assertEqual(payload["job_id"], job_id)
            self.assertEqual(payload["status"], "applied")

    def test_resolve_job_prefix_and_snooze_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            resolved = service.resolve_job_prefix(job_id[:12])
            self.assertEqual(resolved["job_id"], job_id)

            with self.assertRaises(ServiceError) as raised:
                service.resolve_job_prefix("not-a-prefix")
            self.assertEqual(raised.exception.status, 400)

            snoozed = service.snooze(job_id)
            self.assertTrue(snoozed["ok"])
            job = bot.database.get_job(job_id)
            self.assertEqual(job["status"], "snoozed")
            self.assertTrue(job["snoozed_until"])
            action = bot.database.recent_agent_actions(1)[0]
            self.assertEqual(action["kind"], "mark_job")
            self.assertEqual(json.loads(action["payload_json"])["job_id"], job_id)

    def test_query_sql_is_select_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            result = service.query_sql("select title from jobs", limit=5)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["rows"][0]["title"], "AI Product Manager")

            with self.assertRaises(ServiceError) as raised:
                service.query_sql("delete from jobs")
            self.assertEqual(raised.exception.status, 400)

    def test_propose_apply_and_revert_agent_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            before = bot.config.profile_path.read_text(encoding="utf-8")

            proposed = service.propose_actions(
                [
                    {
                        "kind": "directive_edit",
                        "summary": "Prefer AI builder roles",
                        "payload": {"directive": "Prioritize product roles building with Codex or Claude."},
                    }
                ],
                user_intent="tighten scoring",
                session_id="test-session",
            )
            action_id = proposed["actions"][0]["id"]

            applied = service.apply_action(action_id=action_id)
            self.assertTrue(applied["ok"])
            self.assertEqual(bot.database.get_agent_action(action_id)["status"], "applied")
            self.assertIn("Prioritize product roles", bot.config.profile_path.read_text(encoding="utf-8"))

            reverted = service.revert_action(action_id)
            self.assertTrue(reverted["ok"])
            self.assertEqual(bot.config.profile_path.read_text(encoding="utf-8"), before)

    def test_lead_research_digest_mark_and_pitch(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.icp_path.write_text("I help AI-first SaaS teams with workflow automation.", encoding="utf-8")
            service = JobHunterService(bot)

            with mock.patch("jobhunter.service.validate_safe_url"):
                saved = service.research_leads(
                    {
                        "session_id": "lead-session",
                        "user_intent": "find AI founders",
                        "leads": [
                            {
                                "person_name": "Alex Founder",
                                "company": "AgentCo",
                                "role": "Founder",
                                "url": "https://example.com/alex",
                                "evidence": ["Raised Series A for an AI workflow product"],
                                "why_match": "Building AI workflow automation",
                                "confidence": 88,
                            }
                        ],
                    }
                )
                source = service.add_lead_source(
                    {
                        "session_id": "lead-session",
                        "name": "AI Founder Directory",
                        "url": "https://example.com/founders",
                    }
                )

            self.assertEqual(saved["count"], 1)
            self.assertTrue(source["ok"])
            lead_id = saved["saved"][0]["id"]
            digest = service.leads_digest(limit=5)
            self.assertEqual(digest["count"], 1)
            self.assertEqual(digest["leads"][0]["id"], lead_id)

            marked = service.mark_lead(lead_id, "shortlisted")
            self.assertEqual(marked["status"], "shortlisted")

            pitch = service.draft_lead_pitch(lead_id)
            self.assertIn("Hi Alex", pitch["draft"])
            self.assertIn("AgentCo", pitch["draft"])

    def test_resolve_lead_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            with mock.patch("jobhunter.service.validate_safe_url"):
                saved = service.research_leads(
                    {
                        "leads": [
                            {
                                "company": "PrefixCo",
                                "url": "https://example.com/prefixco",
                            }
                        ]
                    }
                )
            lead_id = saved["saved"][0]["id"]

            self.assertEqual(service.resolve_lead_prefix(lead_id[:12])["lead_id"], lead_id)


if __name__ == "__main__":
    unittest.main()
