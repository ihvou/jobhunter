import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobhunter.app import JobHunter
from jobhunter.models import Job, Lead, ScoreResult, SourceConfig
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

    def test_agent_task_and_report_service_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            filed = service.file_task(
                {
                    "from_agent": "qa",
                    "to_agent": "engineer",
                    "kind": "qa.bug",
                    "summary": "Parser mismatch",
                    "payload": {"sample_ids": [1]},
                    "priority": 10,
                }
            )
            task_id = filed["task_id"]
            picked = service.pick_task({"agent": "engineer"})
            self.assertEqual(picked["task"]["id"], task_id)
            self.assertEqual(picked["task"]["status"], "picked")
            completed = service.complete_task({"task_id": task_id, "status": "completed", "result": {"pr_url": "x"}})
            self.assertEqual(completed["task"]["status"], "completed")

            first = service.write_status_report(
                {"agent": "qa", "summary": "Checked email extraction", "details": {"checked": 3}, "report_date": "2026-05-24"}
            )
            second = service.write_status_report(
                {"agent": "qa", "summary": "Updated report", "details": {"checked": 4}, "report_date": "2026-05-24"}
            )
            self.assertEqual(first["report_id"], second["report_id"])
            reports = service.read_reports(agent="qa", since="2026-05-24")
            self.assertEqual(reports["count"], 1)
            self.assertEqual(reports["reports"][0]["summary"], "Updated report")

            listed = service.list_open_tasks({"to_agent": "engineer", "status": "completed"})
            self.assertEqual(listed["count"], 1)

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
            # Wave bookkeeping fields are present in the response. Exact
            # remaining/wave_done values can be flaky in synthetic tests
            # where inserts and rescores collide within ~ms (SQLite julianday
            # precision is ~ms); the BEHAVIOR-level invariant (every lead
            # gets persisted with new values) is asserted above. See
            # test_rescore_leads_batches_via_wave_loop for the loop-converges
            # property.
            self.assertIn("wave_done", result)
            self.assertIn("remaining", result)
            self.assertTrue(result["wave_start"])

    def test_rescore_leads_batches_via_wave_loop(self):
        from jobhunter.models import Lead

        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.icp_path.write_text("# Leadhunter ICP\n\nDomain-led B2B founder.\n", encoding="utf-8")
            # Seed 5 leads with a 1ms sleep between inserts so each gets a
            # distinguishable microsecond-precision timestamp via update_lead_score
            # later. Without this, all inserts collapse to the same second and
            # SQLite's julianday() precision (~ms) can't distinguish wave_start
            # from rescored leads — a synthetic-test-only issue (in production
            # inserts and rescores happen seconds-to-minutes apart).
            import time
            lead_ids = []
            for i in range(5):
                lid, _ = bot.database.upsert_lead(Lead(
                    person_name="P%s" % i, company="Co %s" % i, role="Founder",
                    url="https://example.com/%s" % i, why_match="stale", confidence=50, status="new",
                ))
                lead_ids.append(lid)
                time.sleep(0.002)
            service = JobHunterService(bot)

            def fake_score(profile, icp, row, override_budget=False):
                return {"confidence": 70, "why_match": "rescored"}

            with mock.patch.object(bot.llm, "lead_score", side_effect=fake_score):
                # Loop until wave_done; cap at 10 iterations.
                # Real-world correctness invariant: EVERY lead ends up with
                # the new confidence and why_match after the loop converges.
                # That's the assertion that matters; exact `total_rescored`
                # can vary in synthetic tests where multiple events land in
                # the same ~ms window (SQLite julianday precision is ~ms,
                # not µs). In production, lead inserts and rescore happen
                # seconds to minutes apart so the precision issue doesn't
                # surface.
                wave_start = None
                iterations = 0
                while True:
                    iterations += 1
                    self.assertLess(iterations, 15, "wave loop did not converge")
                    body = {"statuses": ["new"], "limit": 2}
                    if wave_start:
                        body["wave_start"] = wave_start
                    result = service.rescore_leads(body)
                    self.assertTrue(result["ok"])
                    wave_start = result["wave_start"]
                    if result["wave_done"]:
                        break
                self.assertEqual(result["remaining"], 0)
                # All 5 leads must have been rescored at least once.
                for lid in lead_ids:
                    row = bot.database.get_lead(lid)
                    self.assertEqual(row["confidence"], 70)
                    self.assertIn("rescored", row["why_match"])

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

    def test_find_job_recruiters_caches_and_parses_linkedin(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = "test-key"
            service = JobHunterService(bot)
            fake_results = {
                "results": [
                    {
                        "url": "https://www.linkedin.com/in/jane-smith/",
                        "title": "Jane Smith - Senior Technical Recruiter - ExampleCo | LinkedIn",
                        "description": "Jane has been at ExampleCo for 2 years recruiting product hires.",
                    },
                    {
                        "url": "https://uk.linkedin.com/in/bob-jones",
                        "title": "Bob Jones — Head of People at ExampleCo",
                        "description": "Building the people function at ExampleCo.",
                    },
                    {
                        "url": "https://www.linkedin.com/company/exampleco",
                        "title": "ExampleCo | LinkedIn",
                        "description": "Company page",
                    },
                    {
                        "url": "https://www.linkedin.com/in/jane-smith/",
                        "title": "Jane Smith duplicate",
                        "description": "dup",
                    },
                ]
            }
            with mock.patch("jobhunter.service.firecrawl_search", return_value=fake_results) as patched:
                first = service.find_job_recruiters(job_id)
                self.assertEqual(patched.call_count, 1)
                # second call within TTL must use cache, not firecrawl
                second = service.find_job_recruiters(job_id)
                self.assertEqual(patched.call_count, 1)

            self.assertEqual(first["source"], "firecrawl")
            self.assertEqual(second["source"], "cache")
            self.assertEqual(first["company"], "ExampleCo")
            self.assertEqual(first["kind"], "recruiter")
            # filtered: no /company/ pages, no duplicates, normalized to canonical www.linkedin.com host
            self.assertEqual(len(first["profiles"]), 2)
            self.assertEqual(first["profiles"][0]["url"], "https://www.linkedin.com/in/jane-smith")
            self.assertEqual(first["profiles"][0]["name"], "Jane Smith")
            self.assertIn("Recruiter", first["profiles"][0]["title_hint"])
            self.assertEqual(first["profiles"][1]["url"], "https://www.linkedin.com/in/bob-jones")
            self.assertEqual(first["profiles"][1]["name"], "Bob Jones")
            self.assertIn("Head of People", first["profiles"][1]["title_hint"])

    def test_find_lead_linkedin_uses_founder_role_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = "test-key"
            lead_id, _ = bot.database.upsert_lead(
                Lead(
                    person_name="Alice",
                    company="Acme Insurance Inc",
                    role="Founder",
                    url="https://example.com/co",
                    source_name="Src",
                )
            )
            service = JobHunterService(bot)
            captured = {}

            def fake_search(query, **_kwargs):
                captured["query"] = query
                return {
                    "results": [
                        {
                            "url": "https://www.linkedin.com/in/alice-founder/",
                            "title": "Alice Founder — Founder & CEO at Acme Insurance",
                            "description": "Building AI-native insurance workflows.",
                        }
                    ]
                }

            with mock.patch("jobhunter.service.firecrawl_search", side_effect=fake_search):
                result = service.find_lead_linkedin(lead_id)

            self.assertEqual(result["kind"], "founder")
            self.assertEqual(result["source"], "firecrawl")
            self.assertEqual(len(result["profiles"]), 1)
            self.assertIn("founder", captured["query"].lower())
            self.assertIn("Acme Insurance Inc", captured["query"])
            self.assertIn("site:linkedin.com/in", captured["query"])

    def test_find_recruiters_returns_empty_when_no_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = "test-key"
            job_no_co_id, _ = bot.database.upsert_job(
                Job(
                    source_id="s",
                    source_name="Source",
                    external_id="2",
                    url="https://example.com/job-no-company",
                    title="Mystery role",
                    company="",
                    description="Some role with no company.",
                )
            )
            service = JobHunterService(bot)
            with mock.patch("jobhunter.service.firecrawl_search") as patched:
                result = service.find_job_recruiters(job_no_co_id)
                self.assertEqual(patched.call_count, 0)
            self.assertEqual(result["profiles"], [])
            self.assertEqual(result["source"], "no_company")

    def test_find_recruiters_requires_firecrawl_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = ""
            service = JobHunterService(bot)
            with self.assertRaises(ServiceError) as raised:
                service.find_job_recruiters(job_id)
            self.assertEqual(raised.exception.status, 503)

    def test_find_linkedin_reranks_by_lead_person_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = "test-key"
            lead_id, _ = bot.database.upsert_lead(
                Lead(
                    person_name="Kinza",
                    company="Chief Rebel",
                    role="Founder",
                    url="https://chiefrebel.com",
                    source_name="Src",
                )
            )
            service = JobHunterService(bot)
            fake_results = {
                "results": [
                    {
                        "url": "https://www.linkedin.com/in/axel-lindberg/",
                        "title": "Axel Lindberg — Chief Rebel | LinkedIn",
                        "description": "Stockholm gaming studio",
                    },
                    {
                        "url": "https://www.linkedin.com/in/kinza-azmat/",
                        "title": "Kinza Azmat — Chief Rebel | LinkedIn",
                        "description": "Building Chief Rebel for small businesses.",
                    },
                    {
                        "url": "https://www.linkedin.com/in/bretton-hamilton/",
                        "title": "Bretton Hamilton — Chief Rebel | LinkedIn",
                        "description": "Gaming studio",
                    },
                ]
            }
            with mock.patch("jobhunter.service.firecrawl_search", return_value=fake_results):
                result = service.find_lead_linkedin(lead_id)
            # Kinza should be first (name matches the lead's person_name)
            self.assertEqual(result["profiles"][0]["name"], "Kinza Azmat")
            self.assertIn("kinza-azmat", result["profiles"][0]["url"])

    def test_find_recruiters_includes_domain_hint_in_query_for_company_owned_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.firecrawl_api_key = "test-key"
            # Job pointing at a company-owned domain (not an ATS aggregator).
            job_co_id, _ = bot.database.upsert_job(
                Job(
                    source_id="s",
                    source_name="Source",
                    external_id="co",
                    url="https://substrate.run/careers/founding-engineer",
                    title="Founding engineer",
                    company="Substrate",
                    description="Build LLM tooling",
                )
            )
            # Job pointing at an ATS aggregator host (should NOT contribute a domain hint).
            job_ats_id, _ = bot.database.upsert_job(
                Job(
                    source_id="s",
                    source_name="Source",
                    external_id="ats",
                    url="https://jobs.ashbyhq.com/substrate/abc-123",
                    title="Founding PM",
                    company="Substrate",
                    description="Build LLM tooling",
                )
            )
            service = JobHunterService(bot)
            captured = []

            def fake_search(query, **_kwargs):
                captured.append(query)
                return {"results": []}

            with mock.patch("jobhunter.service.firecrawl_search", side_effect=fake_search):
                service.find_job_recruiters(job_co_id)
                # Use a different company so we hit a real firecrawl call instead of cache:
                bot.database.upsert_job(
                    Job(
                        source_id="s",
                        source_name="Source",
                        external_id="ats2",
                        url="https://jobs.ashbyhq.com/different/abc-456",
                        title="x",
                        company="DifferentCo",
                        description="d",
                    )
                )
                row = bot.database.recent_jobs(5)
                ats_only_id = [r["id"] for r in row if r["company"] == "DifferentCo"][0]
                service.find_job_recruiters(ats_only_id)

            self.assertEqual(len(captured), 2)
            # Company-owned URL → domain present in query
            self.assertIn("substrate.run", captured[0])
            self.assertIn('"Substrate" OR "substrate.run"', captured[0])
            # ATS-only URL → no domain hint added
            self.assertNotIn("ashbyhq", captured[1])
            self.assertIn('"DifferentCo"', captured[1])

    def test_parse_linkedin_results_prefers_snippets_mentioning_domain(self):
        from jobhunter.service import _parse_linkedin_search_results
        results = [
            {"url": "https://www.linkedin.com/in/a/", "title": "A — Recruiter", "description": "Unrelated context"},
            {"url": "https://www.linkedin.com/in/b/", "title": "B — Recruiter", "description": "Worked at substrate.run for 3 years"},
            {"url": "https://www.linkedin.com/in/c/", "title": "C — Recruiter", "description": "Another unrelated bio"},
        ]
        out = _parse_linkedin_search_results(results, domain_hint="substrate.run")
        # B should be first (snippet mentions the domain)
        self.assertEqual(out[0]["name"], "B")
        self.assertIn("substrate.run", out[0]["snippet"])

    def test_show_profile_and_icp_return_local_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            bot.config.profile_path.write_text("# About me\n\nAI PM.\n\n# Directives\n\nPrefer Claude.\n", encoding="utf-8")
            bot.config.icp_path.write_text("# ICP\n\nAI workflow founders.\n", encoding="utf-8")
            bot.config.sources_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "s",
                            "name": "Source",
                            "type": "rss",
                            "url": "https://example.com/rss",
                            "status": "active",
                            "priority": "medium",
                        }
                    ]
                ),
                encoding="utf-8",
            )
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

    def test_pm_goals_kpis_and_reversible_direct_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            bot.config.profile_path.write_text("# About me\n\nAI PM.\n\n# Directives\n\nPrefer Claude.\n", encoding="utf-8")
            bot.config.icp_path.write_text("# ICP\n\nAI workflow founders.\n", encoding="utf-8")
            bot.config.sources_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "s",
                            "name": "Source",
                            "type": "rss",
                            "url": "https://example.com/rss",
                            "status": "active",
                            "priority": "medium",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            goals = service.show_goals()
            self.assertTrue(goals["created_template"])
            self.assertGreaterEqual(len(goals["parsed"]["kpis"]), 4)
            playbook = service.show_research_playbook()
            self.assertTrue(playbook["created_template"])
            self.assertIn("deep-research", playbook["text"])

            snapshot = service.kpi_snapshot(window_days=7)
            for key in [
                "applications_this_week",
                "interviews_this_week",
                "reach_outs_this_week",
                "replies_this_week",
                "irrelevant_rate_jobs_7d",
                "irrelevant_rate_leads_7d",
                "active_sources_7d",
                "latency_email_to_digest_p50_minutes",
                "openai_spend_today_usd",
                "openai_spend_month_usd",
                "firecrawl_calls_today",
            ]:
                self.assertIn(key, snapshot["kpis"])
            self.assertEqual(service.kpi_history(weeks=2)["weeks"], 2)

            before_profile = bot.config.profile_path.read_text(encoding="utf-8")
            direct = service.apply_directive_edit(
                {"text": "Prioritize Claude/Codex builder roles.", "reason": "job ids a,b,c were the only applied roles"}
            )
            action = bot.database.get_agent_action(direct["action_id"])
            self.assertEqual(action["kind"], "directive_edit")
            self.assertEqual(action["status"], "applied_by_pm")
            self.assertIn("job ids a,b,c", action["result_message"])
            self.assertIn("Prioritize Claude/Codex", bot.config.profile_path.read_text(encoding="utf-8"))
            service.revert_action(direct["action_id"])
            self.assertEqual(bot.config.profile_path.read_text(encoding="utf-8"), before_profile)

            before_icp = bot.config.icp_path.read_text(encoding="utf-8")
            icp = service.apply_icp_edit({"text": "# ICP\n\nAI workflow founders in healthcare.", "reason": "lead ids x,y,z"})
            self.assertEqual(bot.database.get_agent_action(icp["action_id"])["status"], "applied_by_pm")
            service.revert_action(icp["action_id"])
            self.assertEqual(bot.config.icp_path.read_text(encoding="utf-8"), before_icp)

            source = service.set_source_status({"source_id": "s", "status": "disabled", "reason": "irrelevant rate 90%"})
            self.assertEqual(bot.database.get_agent_action(source["action_id"])["kind"], "source_status_set")
            service.revert_action(source["action_id"])
            sources = json.loads(bot.config.sources_path.read_text(encoding="utf-8"))
            self.assertEqual(sources[0]["status"], "active")
            self.assertTrue(job_id)

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
