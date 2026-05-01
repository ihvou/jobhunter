import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jobbot.app import JobBot
from jobbot.config import AppConfig, CostConfig
from jobbot.database import Database
from jobbot.models import Job, ScoreResult, SourceConfig, TelegramAction


class FakeTelegram:
    enabled = False

    def __init__(self):
        self.messages = []
        self.answers = []
        self.jobs = []

    def send_digest_header(self, title, body=""):
        self.messages.append((title, body))

    def send_job(self, row):
        self.jobs.append(row["id"])

    def send_message(self, text, reply_markup=None):
        self.messages.append((text, reply_markup))

    def answer_callback(self, callback_id, text):
        self.answers.append(text)

    def poll_actions(self):
        return []

    def send_cover_override_prompt(self, job_id, reason):
        self.messages.append(("override", job_id, reason))


def config_for(tmp):
    root = Path(tmp)
    config_dir = root / "config"
    input_dir = root / "input"
    data_dir = root / "data"
    workspace = root / "workspace"
    config_dir.mkdir()
    input_dir.mkdir()
    (config_dir / "sources.json").write_text("[]", encoding="utf-8")
    (config_dir / "scoring.json").write_text('{"rules": [], "thresholds": {"hard_reject_floor": 0}}', encoding="utf-8")
    (config_dir / "profile.example.json").write_text("{}", encoding="utf-8")
    return AppConfig(
        data_dir=data_dir,
        input_dir=input_dir,
        config_dir=config_dir,
        database_path=data_dir / "jobs.sqlite",
        profile_path=input_dir / "profile.local.md",
        cv_path=input_dir / "cv.local.md",
        profile_settings_path=config_dir / "profile.local.json",
        sources_path=config_dir / "sources.json",
        scoring_path=config_dir / "scoring.json",
        workspace_dir=workspace,
        heartbeat_path=data_dir / "heartbeat",
        cost=CostConfig(),
    )


class AppTests(unittest.TestCase):
    def test_bot_collect_callback_submits_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.executor.submit = lambda fn, *args: called.append(fn.__name__)
            bot.handle_action(TelegramAction(scope="bot", action="collect", callback_id="cb"))
            self.assertIn("collect_and_digest", called)

    def test_duplicate_applied_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.database.upsert_sources([SourceConfig(id="s", name="S", type="rss", url="https://example.com/rss")])
            job_id, _ = bot.database.upsert_job(
                Job(source_id="s", source_name="S", external_id="1", url="https://example.com/1", title="AI Engineer", company="C")
            )
            bot.database.save_score(job_id, ScoreResult(score=80, hard_reject=False))
            action = TelegramAction(scope="job", action="applied", target_id=job_id, callback_id="cb")
            bot.handle_action(action)
            bot.handle_action(action)
            rows = bot.database.count_since("job_feedback", datetime(1970, 1, 1), "action = ?", ("applied",))
            self.assertEqual(rows, 1)

    def test_discovery_approval_appends_test_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            session_id = "s1"
            response_path = bot.config.workspace_dir / "discovery" / "response-s1.json"
            response_path.parent.mkdir(parents=True, exist_ok=True)
            response_path.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "name": "Example Jobs",
                                "url": "https://example.com/jobs.json",
                                "type": "json_api",
                                "risk": "low",
                                "validation_notes": "sample fetch ok",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_discovery_run(session_id, "request", "status")
            bot.database.update_discovery_run(session_id, status="done", response_path=str(response_path), candidate_count=1)
            bot.handle_action(TelegramAction(scope="disc", action="approve", target_id=session_id, callback_id="cb"))
            sources = json.loads(bot.config.sources_path.read_text(encoding="utf-8"))
            self.assertEqual(sources[0]["created_by"], "agent")
            self.assertEqual(sources[0]["status"], "test")

    def test_tuning_apply_writes_scoring_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            response_path = bot.config.workspace_dir / "tuning" / "response-t1.json"
            response_path.parent.mkdir(parents=True, exist_ok=True)
            response_path.write_text(
                json.dumps({"version": 2, "rules": [], "thresholds": {"hard_reject_floor": 0}}),
                encoding="utf-8",
            )
            bot.handle_action(TelegramAction(scope="tune", action="apply", target_id="t1", callback_id="cb"))
            rules = json.loads(bot.config.scoring_path.read_text(encoding="utf-8"))
            self.assertEqual(rules["version"], 2)
            self.assertTrue((bot.config.scoring_path.parent / "scoring.v0.json").exists())

    def test_cover_note_budget_overage_sends_override_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.openai_api_key = "test-key"
            config.cost.daily_budget_usd = 0.0
            config.cost.monthly_budget_usd = 0.0
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            bot.database.upsert_sources([SourceConfig(id="s", name="S", type="rss", url="https://example.com/rss")])
            job_id, _ = bot.database.upsert_job(
                Job(source_id="s", source_name="S", external_id="1", url="https://example.com/1", title="AI Engineer", company="C")
            )
            bot.database.save_score(job_id, ScoreResult(score=80, hard_reject=False))
            bot.generate_cover_note(job_id)
            self.assertEqual(bot.telegram.messages[0][0], "override")


if __name__ == "__main__":
    unittest.main()
