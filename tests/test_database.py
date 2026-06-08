import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from jobhunter.database import Database
from jobhunter.models import Job, Lead, ScoreResult, SourceConfig


class DatabaseTests(unittest.TestCase):
    def test_upsert_job_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            job = Job(
                source_id="test",
                source_name="Test",
                external_id="1",
                url="https://example.com/jobs/1",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            job_id, inserted = db.upsert_job(job)
            self.assertTrue(inserted)
            db.save_score(job_id, ScoreResult(score=88, hard_reject=False, reasons=["Good fit"], concerns=[]))
            rows = db.jobs_for_digest(10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["score"], 44)
            self.assertEqual(rows[0]["l1_score"], 44)

    def test_schema_v17_persists_raw_email_agent_tasks_reports_linkedin_cache_and_imap_uids(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            with db.connection() as conn:
                schema_version = conn.execute("select max(version) as version from schema_version").fetchone()["version"]
                email_columns = [row["name"] for row in conn.execute("pragma table_info(email_alert_raw)").fetchall()]
                job_columns = [row["name"] for row in conn.execute("pragma table_info(jobs)").fetchall()]
                task_columns = [row["name"] for row in conn.execute("pragma table_info(agent_tasks)").fetchall()]
                report_columns = [row["name"] for row in conn.execute("pragma table_info(agent_reports)").fetchall()]
                linkedin_columns = [row["name"] for row in conn.execute("pragma table_info(company_linkedin_cache)").fetchall()]
                imap_uid_columns = [row["name"] for row in conn.execute("pragma table_info(imap_processed_uids)").fetchall()]

            self.assertEqual(schema_version, 18)
            self.assertEqual(
                imap_uid_columns,
                ["source_id", "uid", "processed_at"],
            )
            self.assertEqual(
                linkedin_columns,
                ["company_normalized", "kind", "source_company_label", "profiles_json", "fetched_at"],
            )
            self.assertEqual(
                email_columns,
                [
                    "id",
                    "source_id",
                    "message_id",
                    "sender",
                    "subject",
                    "received_at",
                    "raw_html_blob",
                    "raw_text_blob",
                    "parsed_at",
                    "parsed_jobs_count",
                    "parser_version",
                    "first_listed_at",
                    "parse_count",
                    "parser_status",
                    "parser_status_updated_at",
                    "parser_error",
                ],
            )
            self.assertIn("email_alert_id", job_columns)
            self.assertEqual(
                task_columns,
                [
                    "id",
                    "from_agent",
                    "to_agent",
                    "kind",
                    "summary",
                    "payload_json",
                    "status",
                    "priority",
                    "created_at",
                    "picked_at",
                    "completed_at",
                    "result_json",
                ],
            )
            self.assertEqual(
                report_columns,
                ["id", "agent", "report_date", "summary", "details_json", "created_at"],
            )

            email_alert_id, inserted = db.save_email_alert_raw(
                "email-job-alerts",
                "<raw-1>",
                "alerts@example.com",
                "Product jobs",
                "2026-05-20T09:00:00Z",
                "<html><body><a href='https://example.com/job'>AI PM</a></body></html>",
                "AI PM https://example.com/job",
            )
            self.assertTrue(inserted)
            email_alert_id_again, inserted_again = db.save_email_alert_raw(
                "email-job-alerts",
                "<raw-1>",
                "alerts@example.com",
                "Product jobs updated",
                "2026-05-20T10:00:00Z",
                "<html>duplicate</html>",
                "duplicate",
            )
            self.assertEqual(email_alert_id_again, email_alert_id)
            self.assertFalse(inserted_again)

            job = Job(
                source_id="email-job-alerts",
                source_name="Email",
                external_id="raw-1-job",
                url="https://example.com/job",
                title="AI Product Manager",
                company="ExampleCo",
            )
            job.email_alert_id = email_alert_id
            job_id, _inserted = db.upsert_job(job)

            alerts = db.list_email_alerts(limit=5)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["id"], email_alert_id)
            self.assertEqual(alerts[0]["subject"], "Product jobs updated")
            self.assertEqual(alerts[0]["parser_status"], "pending")
            compare = db.email_alert_compare(email_alert_id)
            self.assertIn("<html><body>", compare["raw_html"])
            self.assertIn("AI PM", compare["raw_text"])
            self.assertEqual(compare["jobs"][0]["id"], job_id)
            self.assertEqual(db.unparsed_email_count(), 1)

            task_id = db.file_agent_task("qa", "engineer", "qa.bug", "test", {"sample_ids": [job_id]}, priority=10)
            picked = db.pick_agent_task("engineer")
            self.assertEqual(picked["id"], task_id)
            self.assertEqual(picked["status"], "picked")
            completed = db.complete_agent_task(task_id, "completed", {"result": "ok"})
            self.assertEqual(completed["status"], "completed")

            # Regression: priority must be descending. Filing a low-priority task first
            # and a high-priority task second should yield the high-priority task on pick.
            db.file_agent_task("qa", "engineer", "qa.bug", "low-prio", {}, priority=20)
            high_id = db.file_agent_task("user", "engineer", "engineer.add_source_type", "high-prio", {}, priority=60)
            picked_high = db.pick_agent_task("engineer")
            self.assertEqual(picked_high["id"], high_id, "engineer must pick the highest-priority open task")
            self.assertEqual(picked_high["priority"], 60)
            report_id = db.write_agent_report("qa", "first", {"checked": 1}, report_date="2026-05-24")
            report_id_again = db.write_agent_report("qa", "updated", {"checked": 2}, report_date="2026-05-24")
            self.assertEqual(report_id_again, report_id)
            reports = db.read_agent_reports(agent="qa", since="2026-05-24")
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["summary"], "updated")

    def test_email_first_listed_at_and_parse_count_track_reparses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            email_alert_id, inserted = db.save_email_alert_raw(
                "email-job-alerts",
                "<listed-1>",
                "alerts@example.com",
                "Product jobs",
                "2026-05-20T09:00:00Z",
                "<html><body>AI PM</body></html>",
                "AI PM",
            )
            self.assertTrue(inserted)
            with db.connection() as conn:
                fresh = conn.execute(
                    "select first_listed_at, parse_count, parsed_at from email_alert_raw where id = ?",
                    (email_alert_id,),
                ).fetchone()
            # first_listed_at stamped at insert (~ when the collector first saw
            # the email); parse_count starts at 0, parsed_at null until extraction.
            self.assertTrue(fresh["first_listed_at"])
            self.assertEqual(fresh["parse_count"], 0)
            self.assertIsNone(fresh["parsed_at"])

            db.mark_email_alert_parsed(email_alert_id, 4)
            db.unmark_email_parsed([email_alert_id])
            db.mark_email_alert_parsed(email_alert_id, 5)
            with db.connection() as conn:
                reparsed = conn.execute(
                    "select first_listed_at, parse_count, parsed_at from email_alert_raw where id = ?",
                    (email_alert_id,),
                ).fetchone()
            # first_listed_at is immutable across reparses; parse_count counts the
            # unmark/reparse cycle so the timeliness KPI can exclude it.
            self.assertEqual(reparsed["first_listed_at"], fresh["first_listed_at"])
            self.assertEqual(reparsed["parse_count"], 2)
            self.assertTrue(reparsed["parsed_at"])

    def test_stale_email_parser_lifecycle_surfaces_retries_without_resetting_kpis(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            now = datetime.utcnow().replace(microsecond=0)
            stale_seen = (now - timedelta(hours=7)).isoformat() + "Z"
            old_received = (now - timedelta(days=40)).isoformat() + "Z"
            with db.connection() as conn:
                conn.execute(
                    """
                    insert into email_alert_raw
                        (source_id, message_id, sender, subject, received_at,
                         raw_html_blob, raw_text_blob, parsed_at, parsed_jobs_count,
                         parser_version, first_listed_at, parse_count,
                         parser_status, parser_status_updated_at)
                    values ('email-job-alerts', '<retry>', '', 'retry me', ?,
                            null, null, null, 0, '', ?, 1, 'pending', ?)
                    """,
                    (stale_seen, stale_seen, stale_seen),
                )
                old_id = conn.execute(
                    """
                    insert into email_alert_raw
                        (source_id, message_id, sender, subject, received_at,
                         raw_html_blob, raw_text_blob, parsed_at, parsed_jobs_count,
                         parser_version, first_listed_at, parse_count,
                         parser_status, parser_status_updated_at)
                    values ('email-job-alerts', '<old>', '', 'too old', ?,
                            null, null, null, 0, '', ?, 0, 'pending', ?)
                    """,
                    (old_received, old_received, old_received),
                ).lastrowid

            pending = db.unparsed_email_alerts(10)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["message_id"], "<retry>")
            self.assertEqual(pending[0]["parser_status"], "retrying")
            self.assertEqual(db.stale_unparsed_email_count(), 1)
            with db.connection() as conn:
                retry = conn.execute("select parse_count, parser_status from email_alert_raw where message_id='<retry>'").fetchone()
                old = conn.execute("select parsed_at, parser_status, parser_error from email_alert_raw where id=?", (old_id,)).fetchone()
            self.assertEqual(retry["parse_count"], 1)
            self.assertEqual(retry["parser_status"], "retrying")
            self.assertTrue(old["parsed_at"])
            self.assertEqual(old["parser_status"], "skipped_stale")
            self.assertIn("aged out", old["parser_error"])

    def test_source_failure_backoff_uses_consecutive_recent_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            now = datetime.utcnow().replace(microsecond=0)
            with db.connection() as conn:
                for index in range(3):
                    started = (now - timedelta(minutes=3 - index)).isoformat() + "Z"
                    conn.execute(
                        """
                        insert into source_runs (source_id, started_at, finished_at, error)
                        values ('remoteok', ?, ?, ?)
                        """,
                        (started, started, "timeout"),
                    )

            backoff = db.source_failure_backoff("remoteok", failure_threshold=3)

            self.assertTrue(backoff["active"])
            self.assertEqual(backoff["failure_count"], 3)
            self.assertEqual(backoff["latest_error"], "timeout")
            self.assertTrue(backoff["retry_after"])

            with db.connection() as conn:
                success_at = now.isoformat() + "Z"
                conn.execute(
                    """
                    insert into source_runs (source_id, started_at, finished_at, error)
                    values ('remoteok', ?, ?, null)
                    """,
                    (success_at, success_at),
                )

            cleared = db.source_failure_backoff("remoteok", failure_threshold=3)
            self.assertFalse(cleared["active"])
            self.assertEqual(cleared["failure_count"], 0)

    def test_imap_processed_uids_record_and_load_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()

            # Empty for unknown source
            self.assertEqual(db.imap_processed_uids("source-x"), set())

            inserted = db.record_imap_processed_uids("source-x", [10, 20, 30, 20])  # 20 dup
            self.assertEqual(inserted, 3)
            self.assertEqual(db.imap_processed_uids("source-x"), {10, 20, 30})

            # Insert overlapping batch — only new ones added
            inserted = db.record_imap_processed_uids("source-x", [30, 40, 50])
            self.assertEqual(inserted, 2)  # 40 + 50 new
            self.assertEqual(db.imap_processed_uids("source-x"), {10, 20, 30, 40, 50})

            # Other sources are isolated
            db.record_imap_processed_uids("source-y", [10])
            self.assertEqual(db.imap_processed_uids("source-y"), {10})

    def test_v16_migration_backfills_existing_imap_last_uid(self):
        """A source with imap_last_uid=5 must have UIDs 1..5 marked processed
        after migration so the new collector logic doesn't reprocess them."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            db.upsert_sources(
                [
                    SourceConfig(
                        id="email-test",
                        name="email-test",
                        type="imap",
                        url="imap://test",
                        imap_last_uid=5,
                    )
                ]
            )
            # The upsert above bumps the row; ensure imap_last_uid is set
            with db.connection() as conn:
                conn.execute("update sources set imap_last_uid = 5 where id = 'email-test'")
            # Re-run migrate_v16 explicitly (simulating an upgrade path)
            from jobhunter.database import migrate_v16
            with db.connection() as conn:
                migrate_v16(conn)
            self.assertEqual(db.imap_processed_uids("email-test"), {1, 2, 3, 4, 5})

    def test_total_score_is_generated_from_l1_and_l2(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            with db.connection() as conn:
                total = conn.execute("pragma table_xinfo(jobs)").fetchall()
                total_column = [row for row in total if row["name"] == "total_score"][0]
                self.assertGreater(int(total_column["hidden"]), 0)
            job_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="1", url="https://example.com/generated", title="Generated", company="C")
            )
            db.save_score(job_id, ScoreResult(score=100, hard_reject=False))
            db.save_l2_verdict(job_id, "relevant", "high", "Strong match", [], "test")
            row = db.get_job(job_id)
            self.assertEqual(row["l1_score"], 50)
            self.assertEqual(row["l2_score"], 50)
            self.assertEqual(row["total_score"], 100)
            with self.assertRaises(Exception):
                with db.connection() as conn:
                    conn.execute("update jobs set total_score = 1 where id = ?", (job_id,))

    def test_v9_migrates_agent_medium_sources_to_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            db.upsert_sources(
                [
                    SourceConfig(
                        id="agent-medium",
                        name="Agent Medium",
                        type="rss",
                        url="https://example.com/agent.xml",
                        created_by="agent",
                        risk_level="medium",
                    ),
                    SourceConfig(
                        id="agent-high",
                        name="Agent High",
                        type="rss",
                        url="https://example.com/high.xml",
                        created_by="agent",
                        risk_level="high",
                    ),
                    SourceConfig(
                        id="user-medium",
                        name="User Medium",
                        type="rss",
                        url="https://example.com/user.xml",
                        created_by="user",
                        risk_level="medium",
                    ),
                ]
            )
            with db.connection() as conn:
                conn.execute("delete from schema_version where version >= 9")
            db.init_schema()
            sources = {row["id"]: row for row in db.source_rows()}
            self.assertEqual(sources["agent-medium"]["risk_level"], "low")
            self.assertEqual(sources["agent-high"]["risk_level"], "high")
            self.assertEqual(sources["user-medium"]["risk_level"], "medium")

    def test_cross_source_dedupe_and_no_respam(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            first = Job(
                source_id="remoteok",
                source_name="RemoteOK",
                external_id="1",
                url="https://example.com/jobs/1?utm_source=x#frag",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            second = Job(
                source_id="remotive",
                source_name="Remotive",
                external_id="2",
                url="https://example.com/jobs/1",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            job_id, inserted = db.upsert_job(first)
            self.assertTrue(inserted)
            same_id, inserted = db.upsert_job(second)
            self.assertEqual(job_id, same_id)
            self.assertFalse(inserted)
            db.save_score(job_id, ScoreResult(score=88, hard_reject=False, reasons=["Good fit"], concerns=[]))
            self.assertEqual(len(db.jobs_for_digest(10)), 1)
            db.mark_digested([job_id])
            self.assertEqual(len(db.jobs_for_digest(10)), 0)

    def test_digest_ignores_score_threshold_and_sorts_by_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            low_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="1", url="https://example.com/low", title="Low", company="C")
            )
            high_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="2", url="https://example.com/high", title="High", company="C")
            )
            db.save_score(low_id, ScoreResult(score=30, hard_reject=False))
            db.save_score(high_id, ScoreResult(score=80, hard_reject=False))
            rows = db.jobs_for_digest(10, min_score=50)
            self.assertEqual([row["id"] for row in rows], [high_id, low_id])

    def test_due_snoozed_jobs_sort_by_total_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            snoozed_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="snoozed", url="https://example.com/snoozed", title="Snoozed", company="C")
            )
            fresh_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="fresh", url="https://example.com/fresh", title="Fresh", company="C")
            )
            db.save_score(snoozed_id, ScoreResult(score=95, hard_reject=False))
            db.save_score(fresh_id, ScoreResult(score=70, hard_reject=False))
            db.update_job_status(snoozed_id, "snoozed", snoozed_until="2000-01-01T00:00:00Z")

            rows = db.jobs_for_digest(10)

            self.assertEqual([row["id"] for row in rows], [snoozed_id, fresh_id])

    def test_secondary_dedupe_same_title_company_nearby_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            first_id, inserted = db.upsert_job(
                Job(
                    source_id="wwr",
                    source_name="WWR",
                    external_id="1",
                    url="https://example.com/userwise-services-product-manager",
                    title="Product Manager",
                    company="Userwise Services",
                    posted_at="2026-05-01T00:00:00Z",
                )
            )
            self.assertTrue(inserted)
            second_id, inserted = db.upsert_job(
                Job(
                    source_id="wwr",
                    source_name="WWR",
                    external_id="2",
                    url="https://example.com/userwise-services-product-manager-1",
                    title="Product Manager",
                    company="Userwise Services",
                    posted_at="2026-05-03T00:00:00Z",
                )
            )
            self.assertEqual(first_id, second_id)
            self.assertFalse(inserted)

    def test_upsert_lead_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            lead_id, inserted = db.upsert_lead(
                Lead(
                    person_name="Alex Founder",
                    company="AgentCo",
                    role="Founder",
                    url="https://example.com/alex",
                    evidence=["Raised Series A"],
                    why_match="Building AI workflow tools",
                    confidence=84,
                )
            )

            self.assertTrue(inserted)
            rows = db.leads_for_digest(10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], lead_id)
            self.assertEqual(rows[0]["company"], "AgentCo")
            self.assertEqual(rows[0]["confidence"], 84)

            db.update_lead_status(lead_id, "shortlisted")
            self.assertEqual(db.get_lead(lead_id)["status"], "shortlisted")

    def test_upsert_lead_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            source_id, inserted = db.upsert_lead_source(
                {
                    "name": "AI Founder Directory",
                    "type": "public_directory",
                    "url": "https://example.com/founders",
                    "notes": "Public founder list",
                }
            )

            self.assertTrue(inserted)
            with db.connection() as conn:
                row = conn.execute("select * from lead_sources where id = ?", (source_id,)).fetchone()
            self.assertEqual(row["name"], "AI Founder Directory")


if __name__ == "__main__":
    unittest.main()
