import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from jobhunter.app import JobHunter
from jobhunter.config import AppConfig, CostConfig
from jobhunter.models import Job, ScoreResult, SourceConfig


def config_for(tmp):
    root = Path(tmp)
    config_dir = root / "config"
    input_dir = root / "input"
    data_dir = root / "data"
    config_dir.mkdir()
    input_dir.mkdir()
    (config_dir / "sources.local.json").write_text("[]", encoding="utf-8")
    (config_dir / "scoring.local.json").write_text('{"version": 1, "rules": [], "thresholds": {"hard_reject_floor": 0}}', encoding="utf-8")
    (config_dir / "profile.example.json").write_text("{}", encoding="utf-8")
    return AppConfig(
        data_dir=data_dir,
        input_dir=input_dir,
        config_dir=config_dir,
        database_path=data_dir / "jobs.sqlite",
        profile_path=input_dir / "profile.local.md",
        cv_path=input_dir / "cv.local.md",
        icp_path=input_dir / "icp.local.md",
        goals_path=input_dir / "goals.local.md",
        research_playbook_path=input_dir / "research-playbook.local.md",
        profile_settings_path=config_dir / "profile.local.json",
        sources_path=config_dir / "sources.local.json",
        scoring_path=config_dir / "scoring.local.json",
        heartbeat_path=data_dir / "heartbeat",
        taskcandidates_path=data_dir / "taskcandidates.md",
        cost=CostConfig(),
    )


def add_scored_job(bot, suffix="1", status="new", score=80, title=None, source_id="s"):
    bot.database.upsert_sources([SourceConfig(id=source_id, name="S", type="rss", url="https://example.com/rss")])
    job_id, _ = bot.database.upsert_job(
        Job(
            source_id=source_id,
            source_name="S",
            external_id=suffix,
            url="https://example.com/%s" % suffix,
            title=title or "AI Product Manager %s" % suffix,
            company="C",
            description="Build AI workflows with agents and product teams.",
        )
    )
    bot.database.save_score(job_id, ScoreResult(score=score, hard_reject=False))
    if status != "new":
        bot.database.update_job_status(job_id, status)
    return job_id


class AppTests(unittest.TestCase):
    def test_initialize_is_headless_and_writes_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            bot = JobHunter(config)

            bot.initialize()

            self.assertTrue(config.heartbeat_path.exists())
            self.assertFalse(hasattr(bot, "telegram"))
            self.assertFalse(hasattr(bot, "agent"))

    def test_digest_rows_come_from_ranked_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            low_id = add_scored_job(bot, "low", score=10)
            high_id = add_scored_job(bot, "high", score=90)

            rows = bot.database.jobs_for_digest(10)

            self.assertEqual([row["id"] for row in rows], [high_id, low_id])

    def test_collection_freshness_reports_stale_before_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))

            freshness = bot.collection_freshness()

            self.assertTrue(freshness["queue_is_stale"])
            self.assertIsNone(freshness["queue_last_collected"])

    def test_collect_skips_source_under_failure_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.sources_path.write_text(
                '[{"id":"remoteok","name":"RemoteOK","type":"rss","url":"https://example.com/rss"}]',
                encoding="utf-8",
            )
            bot = JobHunter(config)
            bot.initialize()
            for _ in range(3):
                run_id = bot.database.start_source_run("remoteok")
                bot.database.finish_source_run(run_id, "remoteok", 0, 0, "timeout")

            with mock.patch("jobhunter.app.collect_from_source") as collect_from_source:
                bot.collect()

            collect_from_source.assert_not_called()
            with bot.database.connection() as conn:
                failed_runs = conn.execute(
                    "select count(*) as c from source_runs where source_id = 'remoteok' and error is not null"
                ).fetchone()["c"]
            self.assertEqual(failed_runs, 3)

    def test_email_alert_product_ai_rows_can_enter_l2_below_default_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            source = SourceConfig(id="email-job-alerts", name="Alerts", type="imap", url="imap://job-alerts")
            job = Job(
                source_id=source.id,
                source_name=source.name,
                external_id="1",
                url="https://example.com/job",
                title="Senior Product Manager, Agentic AI",
                company="RelevantCo",
                description="Own agentic AI product direction.",
            )

            self.assertTrue(bot.should_l2_score(source, job, 19, 0))

    def test_community_source_reachability_falls_back_to_firecrawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.firecrawl_api_key = "fc-test"
            bot = JobHunter(config)

            with mock.patch.object(bot, "source_candidate_direct_reachable", return_value=False), mock.patch(
                "jobhunter.app.validate_safe_url"
            ), mock.patch(
                "jobhunter.app.firecrawl_scrape_markdown",
                return_value={"text": "x" * 100, "status": 200},
            ):
                self.assertTrue(bot.source_candidate_reachable("https://jobs.dou.ua/vacancies/", "community", "test"))

    def test_process_email_alert_persists_raw_for_codex_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.sources_path.write_text(
                '[{"id":"email-job-alerts","name":"Email Alerts","type":"imap","url":"imap://job-alerts"}]',
                encoding="utf-8",
            )
            bot = JobHunter(config)
            bot.initialize()

            result = bot.process_email_alert(
                "email-job-alerts",
                "jobs@example.com",
                "Product Manager jobs",
                '<a href="https://example.com/jobs/pm">Senior Product Manager</a>',
                "<message-1>",
            )

            self.assertEqual(result["jobs_found"], 0)
            self.assertEqual(result["inserted"], 0)
            self.assertTrue(result["email_alert_id"])
            self.assertEqual(result["unparsed_email_count"], 1)
            alerts = bot.database.unparsed_email_alerts(5)
            self.assertEqual(len(alerts), 1)
            self.assertIn("Senior Product Manager", alerts[0]["raw_html"])
            self.assertEqual(bot.database.recent_jobs(5), [])


    def test_ingest_jobs_skips_aged_out_jobs(self):
        """Jobs whose posted_at is older than JOBHUNTER_JOB_MAX_AGE_DAYS get
        silently dropped at ingestion. Recent + NULL posted_at jobs land."""
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            bot = JobHunter(config)
            bot.initialize()
            from jobhunter.models import Job, SourceConfig
            source = SourceConfig(id="rss", name="RSS", type="rss", url="https://example.com/rss")
            jobs = [
                # Old: posted ~2 years ago — should be skipped
                Job(
                    source_id="rss", source_name="RSS", external_id="old",
                    url="https://example.com/jobs/old", title="Stale Product Manager",
                    company="OldCo", description="archive thread",
                    posted_at="2024-01-01T00:00:00+00:00",
                ),
                # Recent: posted 1 day ago — should land
                Job(
                    source_id="rss", source_name="RSS", external_id="new",
                    url="https://example.com/jobs/new", title="Fresh Product Manager",
                    company="NewCo", description="just posted",
                    posted_at=(datetime.utcnow() - timedelta(days=1)).isoformat() + "+00:00",
                ),
                # Null posted_at: should land (we don't know the age)
                Job(
                    source_id="rss", source_name="RSS", external_id="null",
                    url="https://example.com/jobs/null", title="Null-Date Product Manager",
                    company="UnknownCo", description="no posted_at",
                    posted_at=None,
                ),
            ]
            with mock.patch.dict(os.environ, {"JOBHUNTER_JOB_MAX_AGE_DAYS": "30"}, clear=False):
                result = bot.ingest_jobs(source, jobs)
            self.assertEqual(result["aged_out"], 1)
            self.assertEqual(result["inserted"], 2)
            ids = {row["external_id"] for row in bot.database.recent_jobs(10)}
            self.assertEqual(ids, {"new", "null"})

    def test_ingest_jobs_drops_email_source_null_posted_at(self):
        """Email digest sources aggregate jobs of varying ages. A recent email
        can contain a 5-month-old job (Djinni keeps it in alerts until filled).
        We drop NULL posted_at from email sources to avoid showing stale jobs.
        Non-email sources keep NULL = visible.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            bot = JobHunter(config)
            bot.initialize()
            from jobhunter.models import Job, SourceConfig
            jobs = [
                Job(
                    source_id="email-job-alerts", source_name="Email",
                    external_id="email-null", url="https://djinni.co/jobs/794725-ai/",
                    title="AI/ML Product Engineer", company="Inception-1",
                    description="extracted from email, Codex couldn't find a date",
                    posted_at=None,
                ),
                Job(
                    source_id="rss", source_name="RSS",
                    external_id="rss-null", url="https://example.com/jobs/rss-null",
                    title="Fresh PM from RSS", company="RssCo",
                    description="listing page shows recent content",
                    posted_at=None,
                ),
            ]
            email_source = SourceConfig(id="email-job-alerts", name="Email", type="imap", url="imap://job-alerts")
            rss_source = SourceConfig(id="rss", name="RSS", type="rss", url="https://example.com/rss")
            with mock.patch.dict(os.environ, {"JOBHUNTER_JOB_MAX_AGE_DAYS": "30"}, clear=False):
                email_result = bot.ingest_jobs(email_source, [jobs[0]])
                rss_result = bot.ingest_jobs(rss_source, [jobs[1]])
            # Email-source NULL: dropped
            self.assertEqual(email_result["aged_out"], 1)
            self.assertEqual(email_result["inserted"], 0)
            # RSS-source NULL: kept
            self.assertEqual(rss_result["aged_out"], 0)
            self.assertEqual(rss_result["inserted"], 1)

    def test_ingest_jobs_disabled_when_max_age_is_zero(self):
        """Setting JOBHUNTER_JOB_MAX_AGE_DAYS=0 disables the cutoff entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            bot = JobHunter(config)
            bot.initialize()
            from jobhunter.models import Job, SourceConfig
            source = SourceConfig(id="rss", name="RSS", type="rss", url="https://example.com/rss")
            jobs = [
                Job(
                    source_id="rss", source_name="RSS", external_id="old",
                    url="https://example.com/jobs/old", title="Stale Product Manager",
                    company="OldCo", description="archive",
                    posted_at="2024-01-01T00:00:00+00:00",
                ),
            ]
            with mock.patch.dict(os.environ, {"JOBHUNTER_JOB_MAX_AGE_DAYS": "0"}, clear=False):
                result = bot.ingest_jobs(source, jobs)
            self.assertEqual(result["aged_out"], 0)
            self.assertEqual(result["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
