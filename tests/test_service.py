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

    def test_email_alert_audit_endpoints_return_raw_and_joined_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.sources_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "email-job-alerts",
                            "name": "Email Alerts",
                            "type": "imap",
                            "url": "imap://job-alerts",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            service = JobHunterService(bot)

            processed = service.process_email(
                {
                    "source_id": "email-job-alerts",
                    "sender": "alerts@example.com",
                    "subject": "Product jobs",
                    "message_id": "<service-email-1>",
                    "date": "Wed, 20 May 2026 09:00:00 +0000",
                    "body": '<html><body><a href="https://example.com/email-job">AI Product Manager</a></body></html>',
                }
            )

            self.assertTrue(processed["ok"])
            self.assertTrue(processed["raw_inserted"])
            email_alert_id = processed["email_alert_id"]
            listed = service.list_email_alerts(limit=5)
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["alerts"][0]["id"], email_alert_id)
            self.assertNotIn("raw_html_blob", listed["alerts"][0])

            compared = service.email_alert_compare(email_alert_id)
            email_alert = compared["email_alert"]
            self.assertIn("<html><body>", email_alert["raw_html"])
            self.assertEqual(email_alert["raw_text"], "")
            self.assertEqual(email_alert["jobs"], [])

            pending = service.unparsed_emails(limit=20)
            self.assertEqual(pending["count"], 1)
            self.assertIn("AI Product Manager", pending["emails"][0]["raw_html"])
            enrichment_html = """
<script type="application/ld+json">
{"@type":"JobPosting","title":"AI Product Manager","description":"Build AI workflow products with agents, automation, customer discovery, roadmaps, experiments, and cross-functional product teams. This longer text proves URL enrichment happened after Codex extraction.","hiringOrganization":{"name":"ExampleCo"},"jobLocation":{"address":{"addressLocality":"Remote"}}}
</script>
"""
            with mock.patch("jobhunter.service.validate_safe_url"), mock.patch("jobhunter.sources.fetch_text", return_value=enrichment_html):
                saved = service.save_extracted_email_jobs(
                    {
                        "email_alert_id": email_alert_id,
                        "jobs": [
                            {
                                "title": "AI Product Manager role at ExampleCo is available",
                                "company": "ExampleCo LinkedIn",
                                "url": "https://example.com/email-job",
                                "snippet": "Short snippet",
                            }
                        ],
                    }
                )
            self.assertEqual(saved["saved"], 1)
            self.assertEqual(saved["enriched"], 1)
            compared = service.email_alert_compare(email_alert_id)["email_alert"]
            self.assertEqual(compared["parsed_jobs_count"], 1)
            self.assertEqual(len(compared["jobs"]), 1)
            self.assertEqual(compared["jobs"][0]["email_alert_id"], email_alert_id)
            self.assertGreater(len(compared["jobs"][0]["description"]), 100)

    def test_rescore_leads_updates_confidence_and_why_match(self):
        from jobhunter.models import Lead

        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.icp_path.write_text(
                "# Leadhunter ICP\n\nDomain-led non-technical founder, early B2B, execution gap.\n",
                encoding="utf-8",
            )
            # Two leads with deliberately stale rationale (older workflow framing).
            lead1_id, _ = bot.database.upsert_lead(Lead(
                person_name="Alice Founder", company="Domain Co", role="Founder",
                url="https://example.com/alice", why_match="Strong workflow automation signal",
                confidence=80, status="new",
            ))
            lead2_id, _ = bot.database.upsert_lead(Lead(
                person_name="Bob TechBuilder", company="DevTools Inc", role="Founder/CTO",
                url="https://example.com/bob", why_match="Workflow product with engineering team",
                confidence=72, status="new",
            ))
            service = JobHunterService(bot)

            # Mock LLMClient.lead_score so the test doesn't require an OPENAI_API_KEY.
            def fake_score(profile, icp, row, override_budget=False):
                if "Domain Co" in (row["company"] or ""):
                    return {"confidence": 85, "why_match": "Non-technical domain-led founder; ICP fit."}
                if "DevTools" in (row["company"] or ""):
                    return {"confidence": 30, "why_match": "Strong technical team; out of current ICP."}
                return None
            with mock.patch.object(bot.llm, "lead_score", side_effect=fake_score):
                result = service.rescore_leads({"statuses": ["new"], "limit": 10})

            self.assertTrue(result["ok"])
            self.assertEqual(result["rescored"], 2)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["errors_count"], 0)
            # Persisted values match the mocked scores.
            self.assertEqual(bot.database.get_lead(lead1_id)["confidence"], 85)
            self.assertIn("Non-technical domain-led", bot.database.get_lead(lead1_id)["why_match"])
            self.assertEqual(bot.database.get_lead(lead2_id)["confidence"], 30)
            self.assertIn("Strong technical team", bot.database.get_lead(lead2_id)["why_match"])
            # Audit row was recorded with the run summary.
            audit = bot.database.recent_agent_actions(5)
            kinds = [row["kind"] for row in audit]
            self.assertIn("lead_rescore", kinds)
            # Biggest movers ordered by abs(delta); Bob (-42) outranks Alice (+5).
            movers = result["biggest_movers"]
            self.assertEqual(movers[0]["company"], "DevTools Inc")
            self.assertEqual(movers[0]["delta"], -42)
            self.assertEqual(movers[1]["company"], "Domain Co")

    def test_rescore_leads_rejects_unknown_status_and_empty_icp(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.icp_path.write_text("# Leadhunter ICP\n\nProvided.\n", encoding="utf-8")
            service = JobHunterService(bot)
            with self.assertRaises(ServiceError) as raised:
                service.rescore_leads({"statuses": ["totally-fake-status"]})
            self.assertEqual(raised.exception.status, 400)

            bot.config.icp_path.write_text("", encoding="utf-8")
            with self.assertRaises(ServiceError) as raised:
                service.rescore_leads({})
            self.assertEqual(raised.exception.status, 400)
            self.assertIn("ICP is empty", raised.exception.message)

    def test_show_profile_and_icp_return_local_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.profile_path.write_text("# About me\n\nAI PM.\n\n# Directives\n\nPrefer Claude.\n", encoding="utf-8")
            bot.config.icp_path.write_text("# ICP\n\nAI workflow founders.\n", encoding="utf-8")
            service = JobHunterService(bot)

            profile = service.show_profile()
            self.assertTrue(profile["ok"])
            self.assertIn("AI PM", profile["text"])
            self.assertEqual(profile["about_me"], "AI PM.")
            self.assertEqual(profile["directives"], "Prefer Claude.")

            icp = service.show_icp()
            self.assertTrue(icp["ok"])
            self.assertTrue(icp["exists"])
            self.assertIn("AI workflow founders", icp["text"])

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
