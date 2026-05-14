import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
