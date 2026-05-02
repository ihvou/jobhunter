import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jobbot.app import JobBot
from jobbot.config import AppConfig, CostConfig
from jobbot.database import Database
from jobbot.models import Job, ScoreResult, SourceConfig, TelegramAction
from jobbot.telegram import TelegramError


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

    def send_long_message(self, text):
        self.messages.append((text, None))

    def send_agent_response(self, session_id, response):
        self.messages.append(("agent", session_id, response))

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


def add_scored_job(bot, suffix="1", status="new", score=80):
    bot.database.upsert_sources([SourceConfig(id="s", name="S", type="rss", url="https://example.com/rss")])
    job_id, _ = bot.database.upsert_job(
        Job(
            source_id="s",
            source_name="S",
            external_id=suffix,
            url="https://example.com/%s" % suffix,
            title="AI Engineer %s" % suffix,
            company="C",
        )
    )
    bot.database.save_score(job_id, ScoreResult(score=score, hard_reject=False))
    if status != "new":
        bot.database.update_job_status(job_id, status)
    return job_id


class AppTests(unittest.TestCase):
    def test_bot_collect_callback_submits_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append(fn.__name__)
            bot.handle_action(TelegramAction(scope="bot", action="collect", callback_id="cb"))
            self.assertIn("collect_and_digest", called)

    def test_bot_collect_reply_keyboard_message_acknowledges_in_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append(fn.__name__)
            bot.handle_action(TelegramAction(scope="bot", action="collect"))
            self.assertIn("collect_and_digest", called)
            self.assertIn(("Searching for new jobs...", None), bot.telegram.messages)

    def test_bot_menu_action_sends_reply_keyboard_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="menu"))
            self.assertEqual(bot.telegram.messages[-1], ("Jobbot ready", "Use the keyboard buttons below."))

    def test_bot_header_callbacks_write_agent_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="discover_sources", callback_id="cb"))
            bot.handle_action(TelegramAction(scope="bot", action="tune_scoring", callback_id="cb"))
            bot.handle_action(TelegramAction(scope="bot", action="usage", callback_id="cb"))
            self.assertEqual(len(list((bot.config.workspace_dir / "agent").glob("request-*.json"))), 1)
            self.assertTrue(any("Agent request queued" in str(message[0]) for message in bot.telegram.messages))

    def test_send_digest_filters_by_min_show_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 50}}', encoding="utf-8")
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            low_id = add_scored_job(bot, "low", score=30)
            high_id = add_scored_job(bot, "high", score=80)

            bot.send_digest()

            self.assertEqual(bot.telegram.jobs, [high_id])
            self.assertNotIn(low_id, bot.telegram.jobs)

        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 90}}', encoding="utf-8")
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            add_scored_job(bot, "below", score=80)

            bot.send_digest()

            self.assertEqual(bot.telegram.jobs, [])
            self.assertEqual(bot.telegram.messages[0][0], "No strong new matches right now")

    def test_l2_relevance_filters_obvious_bad_role_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 50}}', encoding="utf-8")
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            good_id = add_scored_job(bot, "good", score=80)
            bad_id, _ = bot.database.upsert_job(
                Job(
                    source_id="s",
                    source_name="S",
                    external_id="bad",
                    url="https://example.com/bad",
                    title="Product Marketing Manager",
                    company="C",
                    description="Own launches and messaging.",
                )
            )
            bot.database.save_score(bad_id, ScoreResult(score=80, hard_reject=False))

            bot.send_digest()

            self.assertIn(good_id, bot.telegram.jobs)
            self.assertNotIn(bad_id, bot.telegram.jobs)

    def test_job_feedback_callbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append((fn.__name__, args))
            job_id = add_scored_job(bot, "irrelevant")
            bot.handle_action(TelegramAction(scope="job", action="irrelevant", target_id=job_id, callback_id="cb"))
            self.assertEqual(bot.database.get_job(job_id)["status"], "rejected")

            job_id = add_scored_job(bot, "snooze")
            bot.handle_action(TelegramAction(scope="job", action="snooze_1d", target_id=job_id, callback_id="cb"))
            self.assertEqual(bot.database.get_job(job_id)["status"], "snoozed")

            job_id = add_scored_job(bot, "cover")
            bot.handle_action(TelegramAction(scope="job", action="cover_note", target_id=job_id, callback_id="cb"))
            self.assertIn(("generate_cover_note", (job_id, False)), called)

    def test_duplicate_applied_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            job_id = add_scored_job(bot)
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
            with mock.patch("jobbot.app.validate_safe_url"):
                bot.source_candidate_reachable = lambda url: True
                bot.handle_action(TelegramAction(scope="disc", action="approve", target_id=session_id, callback_id="cb"))
            sources = json.loads(bot.config.sources_path.read_text(encoding="utf-8"))
            self.assertEqual(sources[0]["created_by"], "agent")
            self.assertEqual(sources[0]["status"], "test")
            self.assertEqual(bot.telegram.answers[-1], "Approved 1, skipped 0")

    def test_discovery_approval_reports_counts_and_sanitizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.sources_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "existing",
                            "name": "Existing",
                            "type": "json_api",
                            "url": "https://example.com/jobs.json",
                            "enabled": True,
                            "status": "active",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            session_id = "s1"
            response_path = bot.config.workspace_dir / "discovery" / "response-s1.json"
            response_path.parent.mkdir(parents=True, exist_ok=True)
            response_path.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {"name": "Duplicate", "url": "https://example.com/jobs.json", "type": "json_api"},
                            {
                                "name": "<script>alert(1)</script>",
                                "url": "https://example.org/jobs.json",
                                "type": "json_api",
                                "why_it_matches": "<b>good</b>",
                                "validation_notes": "<script>ok</script>",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_discovery_run(session_id, "request", "status")
            bot.database.update_discovery_run(session_id, status="done", response_path=str(response_path), candidate_count=2)
            with mock.patch("jobbot.app.validate_safe_url"):
                bot.source_candidate_reachable = lambda url: True
                bot.handle_action(TelegramAction(scope="disc", action="approve", target_id=session_id, callback_id="cb"))
            sources = json.loads(config.sources_path.read_text(encoding="utf-8"))
            self.assertEqual(len(sources), 2)
            self.assertEqual(bot.telegram.answers[-1], "Approved 1, skipped 1")
            written = json.dumps(sources[-1])
            self.assertNotIn("<", written)
            self.assertNotIn("script", written.lower())

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

    def test_invalid_tuning_rules_do_not_overwrite_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            response_path = bot.config.workspace_dir / "tuning" / "response-bad.json"
            response_path.parent.mkdir(parents=True, exist_ok=True)
            response_path.write_text(json.dumps({"version": 2, "thresholds": {}}), encoding="utf-8")
            before = bot.config.scoring_path.read_bytes()

            bot.handle_action(TelegramAction(scope="tune", action="apply", target_id="bad", callback_id="cb"))

            self.assertEqual(bot.config.scoring_path.read_bytes(), before)
            self.assertFalse((bot.config.scoring_path.parent / "scoring.v0.json").exists())
            self.assertIn("Invalid ruleset, scoring unchanged", bot.telegram.answers[-1])

    def test_tuning_worker_failure_is_sent_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            status_path = bot.config.workspace_dir / "tuning" / "status-failed1.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(json.dumps({"state": "failed", "message": "timeout"}), encoding="utf-8")

            bot.poll_workspace()
            bot.poll_workspace()

            messages = [message[0] for message in bot.telegram.messages]
            failures = [message for message in messages if "Scoring tuning failed" in message]
            self.assertEqual(failures, ["Scoring tuning failed for session failed1: timeout"])
            self.assertTrue((bot.config.workspace_dir / "tuning" / "notified-failed1").exists())

    def test_cover_note_budget_overage_sends_override_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.openai_api_key = "test-key"
            config.cost.daily_budget_usd = 0.0
            config.cost.monthly_budget_usd = 0.0
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            job_id = add_scored_job(bot)
            bot.generate_cover_note(job_id)
            self.assertEqual(bot.telegram.messages[0][0], "override")

    def test_cover_note_skips_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append((fn.__name__, args))
            applied_job_id = add_scored_job(bot, "applied-cover", status="applied")
            bot.handle_action(TelegramAction(scope="job", action="cover_note", target_id=applied_job_id, callback_id="cb"))
            self.assertFalse(called)
            self.assertEqual(bot.telegram.answers[-1], "Already applied - cover note skipped")

            bot.generate_cover_note(applied_job_id)
            self.assertEqual(bot.database.get_job(applied_job_id)["status"], "applied")
            self.assertIn("Cover note skipped", bot.telegram.messages[-1][0])

    def test_collection_shutdown_marks_source_run_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.shutdown_requested = True
            source = SourceConfig(id="slow", name="Slow", type="rss", url="https://example.com/rss")
            bot.database.upsert_sources([source])
            bot.collect_source(source, {"rules": [], "thresholds": {"hard_reject_floor": 0}})
            with bot.database.connection() as conn:
                row = conn.execute("select error from source_runs where source_id = ?", ("slow",)).fetchone()
            self.assertEqual(row["error"], "interrupted")

    def test_telegram_errors_do_not_crash_poll_loop(self):
        class ErrorTelegram(FakeTelegram):
            enabled = True

            def poll_actions(self):
                return [TelegramAction(scope="bot", action="usage", callback_id="cb")]

            def answer_callback(self, callback_id, text):
                raise TelegramError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            bot = JobBot(config_for(tmp))
            bot.telegram = ErrorTelegram()
            bot.poll_telegram_once()

    def test_discovery_request_excludes_sensitive_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.sources_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "private-api",
                            "name": "Private API",
                            "type": "json_api",
                            "url": "https://example.com/jobs.json",
                            "headers": {"Authorization": "Bearer secret"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="discover_sources", callback_id="cb"))
            request_path = next((config.workspace_dir / "agent").glob("request-*.json"))
            request = request_path.read_text(encoding="utf-8")
            self.assertNotIn("Authorization", request)
            self.assertNotIn("secret", request)


if __name__ == "__main__":
    unittest.main()
