import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jobhunter.app import JobHunter
from jobhunter.agent_actions import ActionResult
from jobhunter.config import AppConfig, CostConfig
from jobhunter.database import Database
from jobhunter.models import Job, ScoreResult, SourceConfig, TelegramAction
from jobhunter.telegram import TelegramError, format_agent_response


class FakeTelegram:
    enabled = False

    def __init__(self):
        self.messages = []
        self.answers = []
        self.jobs = []
        self.deleted = []
        self.edits = []
        self.fail_next_edit = False

    def send_digest_header(self, title, body=""):
        self.messages.append((title, body))

    def send_job(self, row):
        self.jobs.append(row["id"])

    def send_message(self, text, reply_markup=None):
        self.messages.append((text, reply_markup))
        return {"message_id": len(self.messages)}

    def send_long_message(self, text, reply_markup=None):
        self.messages.append((text, reply_markup))
        return {"message_id": len(self.messages)}

    def send_agent_response(self, session_id, response, message_id=None):
        text, keyboard = format_agent_response(session_id, response)
        if message_id:
            try:
                self.edit_message_text(message_id, text, keyboard)
                return
            except TelegramError:
                self.delete_message(message_id)
        self.send_long_message(text, keyboard)

    def edit_message_text(self, message_id, text, reply_markup=None):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise TelegramError("edit failed")
        self.edits.append((message_id, text, reply_markup))
        return {"message_id": message_id}

    def answer_callback(self, callback_id, text):
        self.answers.append(text)

    def delete_message(self, message_id):
        self.deleted.append(message_id)

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
        tasks_path=root / "tasks.md",
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
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append(fn.__name__)
            bot.handle_action(TelegramAction(scope="bot", action="collect", callback_id="cb"))
            self.assertIn("collect_and_digest", called)

    def test_bot_collect_reply_keyboard_message_acknowledges_in_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append(fn.__name__)
            bot.handle_action(TelegramAction(scope="bot", action="collect"))
            self.assertIn("collect_and_digest", called)
            self.assertIn(("Searching for new jobs...", None), bot.telegram.messages)

    def test_bot_menu_action_sends_reply_keyboard_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="menu"))
            self.assertEqual(bot.telegram.messages[-1], ("Jobhunter ready", "Use the keyboard buttons below."))

    def test_bot_header_callbacks_write_agent_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="discover_sources", callback_id="cb"))
            bot.handle_action(TelegramAction(scope="bot", action="tune_scoring", callback_id="cb"))
            bot.handle_action(TelegramAction(scope="bot", action="usage", callback_id="cb"))
            self.assertEqual(len(list((bot.config.workspace_dir / "agent").glob("request-*.json"))), 1)
            self.assertTrue(any("Processing your request" in str(message[0]) for message in bot.telegram.messages))

    def test_agent_request_log_includes_sanitized_user_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            with self.assertLogs("jobhunter.agent", level="INFO") as captured:
                bot.handle_action(TelegramAction(scope="bot", action="agent", text="test question\nwith newline\x00"))
            contexts = [getattr(record, "_context", {}) for record in captured.records]
            self.assertTrue(any(context.get("user_text") == "test question with newline" for context in contexts))

    def test_agent_request_placeholder_edits_to_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="agent", text="show usage"))
            run = bot.database.active_agent_run()
            placeholder_id = run["placeholder_message_id"]
            self.assertEqual(bot.telegram.messages[-1][0].splitlines()[0], "Processing your request: 'show usage'...")

            status_path = Path(run["status_path"])
            response_path = bot.config.workspace_dir / "agent" / ("response-%s.json" % run["session_id"])
            status_path.write_text(json.dumps({"state": "done"}), encoding="utf-8")
            response_path.write_text(json.dumps({"answer": "Final answer", "proposed_actions": []}), encoding="utf-8")

            bot.poll_workspace()

            self.assertEqual(bot.telegram.edits[-1][0], placeholder_id)
            self.assertIn("Final answer", bot.telegram.edits[-1][1])

    def test_agent_response_edit_falls_back_to_delete_and_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="agent", text="show sources"))
            run = bot.database.active_agent_run()
            placeholder_id = run["placeholder_message_id"]
            bot.telegram.fail_next_edit = True
            Path(run["status_path"]).write_text(json.dumps({"state": "done"}), encoding="utf-8")
            response_path = bot.config.workspace_dir / "agent" / ("response-%s.json" % run["session_id"])
            response_path.write_text(json.dumps({"answer": "Fallback answer", "proposed_actions": []}), encoding="utf-8")

            bot.poll_workspace()

            self.assertIn(placeholder_id, bot.telegram.deleted)
            self.assertIn("Fallback answer", bot.telegram.messages[-1][0])

    def test_duplicate_agent_request_is_blocked_but_safe_flows_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append(fn.__name__)

            bot.handle_action(TelegramAction(scope="bot", action="agent", text="first request"))
            bot.handle_action(TelegramAction(scope="bot", action="agent", text="second request"))
            bot.handle_action(TelegramAction(scope="bot", action="collect"))
            bot.handle_action(TelegramAction(scope="bot", action="usage"))

            requests = list((bot.config.workspace_dir / "agent").glob("request-*.json"))
            self.assertEqual(len(requests), 1)
            self.assertTrue(any("Still processing your previous request" in str(message[0]) for message in bot.telegram.messages))
            self.assertIn("collect_and_digest", called)
            self.assertIn("Usage", bot.telegram.messages[-1][0])

    def test_send_digest_filters_by_min_show_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 50}}', encoding="utf-8")
            bot = JobHunter(config)
            bot.telegram = FakeTelegram()
            low_id = add_scored_job(bot, "low", score=30)
            high_id = add_scored_job(bot, "high", score=80)

            bot.send_digest()

            self.assertEqual(bot.telegram.jobs, [high_id])
            self.assertNotIn(low_id, bot.telegram.jobs)

        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 90}}', encoding="utf-8")
            bot = JobHunter(config)
            bot.telegram = FakeTelegram()
            add_scored_job(bot, "below", score=80)

            bot.send_digest()

            self.assertEqual(bot.telegram.jobs, [])
            self.assertEqual(bot.telegram.messages[0][0], "No strong new matches right now")

    def test_l2_relevance_filters_obvious_bad_role_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.scoring_path.write_text('{"rules": [], "thresholds": {"min_show_score": 50}}', encoding="utf-8")
            bot = JobHunter(config)
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
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            called = []
            bot.submit_background = lambda fn, *args: called.append((fn.__name__, args))
            job_id = add_scored_job(bot, "irrelevant")
            bot.handle_action(TelegramAction(scope="job", action="irrelevant", target_id=job_id, callback_id="cb", message_id=101))
            self.assertEqual(bot.database.get_job(job_id)["status"], "rejected")
            self.assertIn(101, bot.telegram.deleted)

            job_id = add_scored_job(bot, "snooze")
            bot.handle_action(TelegramAction(scope="job", action="snooze_1d", target_id=job_id, callback_id="cb", message_id=102))
            self.assertEqual(bot.database.get_job(job_id)["status"], "snoozed")
            self.assertIn(102, bot.telegram.deleted)

            job_id = add_scored_job(bot, "cover")
            bot.handle_action(TelegramAction(scope="job", action="cover_note", target_id=job_id, callback_id="cb", message_id=103))
            self.assertIn(("generate_cover_note", (job_id, False)), called)
            self.assertIn(103, bot.telegram.deleted)

    def test_job_status_commands_return_deleted_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            add_scored_job(bot, "applied-list", status="applied")

            bot.handle_action(TelegramAction(scope="bot", action="list_applied"))

            self.assertIn("Recent applied jobs", bot.telegram.messages[-1][0])
            self.assertIn("https://example.com/applied-list", bot.telegram.messages[-1][0])

    def test_duplicate_applied_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            job_id = add_scored_job(bot)
            action = TelegramAction(scope="job", action="applied", target_id=job_id, callback_id="cb")
            bot.handle_action(action)
            bot.handle_action(action)
            rows = bot.database.count_since("job_feedback", datetime(1970, 1, 1), "action = ?", ("applied",))
            self.assertEqual(rows, 1)

    def test_bulk_agent_action_requires_and_accepts_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            for index in range(12):
                add_scored_job(bot, "bulk-%s" % index)
            session_id = "bulk1"
            agent_dir = bot.config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "response-bulk1.json").write_text(
                json.dumps(
                    {
                        "answer": "Archive old matches",
                        "proposed_actions": [
                            {
                                "kind": "bulk_update_jobs",
                                "summary": "Archive all jobs",
                                "payload": {"filter_sql": "select id from jobs", "new_status": "archived"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_agent_run(session_id, "archive jobs", str(agent_dir / "request-bulk1.json"), str(agent_dir / "status-bulk1.json"))

            bot.handle_action(TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb"))
            pending = bot.database.recent_agent_actions(1)[0]
            self.assertEqual(pending["status"], "pending_confirm")
            self.assertIn("CONFIRM", bot.telegram.messages[-1][0])

            bot.handle_action(TelegramAction(scope="bot", action="confirm", target_id=str(pending["id"])))
            confirmed = bot.database.get_agent_action(pending["id"])
            self.assertEqual(confirmed["status"], "applied")
            with bot.database.connection() as conn:
                archived = conn.execute("select count(*) as c from jobs where status = 'archived'").fetchone()["c"]
            self.assertEqual(archived, 12)

    def test_email_parser_proposal_agent_action_persists_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            session_id = "email1"
            agent_dir = bot.config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "response-email1.json").write_text(
                json.dumps(
                    {
                        "answer": "Add parser",
                        "proposed_actions": [
                            {
                                "kind": "email_parser_proposal",
                                "summary": "Parse LinkedIn alert cards",
                                "payload": {
                                    "template": {
                                        "id": "linkedin-alerts",
                                        "source_id": "email-job-alerts",
                                        "sender_pattern": "linkedin",
                                        "subject_pattern": "jobs",
                                        "parser_config": {"max_jobs": 5},
                                    }
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_agent_run(session_id, "add email parser", str(agent_dir / "request-email1.json"), str(agent_dir / "status-email1.json"))

            bot.handle_action(TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb"))

            templates = bot.database.email_templates_for_source("email-job-alerts")
            self.assertEqual(templates[0]["id"], "linkedin-alerts")
            self.assertEqual(templates[0]["parser_config"]["max_jobs"], 5)

    def test_agent_apply_acknowledges_callback_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            session_id = "ack1"
            agent_dir = bot.config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "response-ack1.json").write_text(
                json.dumps(
                    {
                        "answer": "Apply slow action",
                        "proposed_actions": [
                            {"kind": "directive_edit", "summary": "Slow", "payload": {"directive": "Skip duplicates"}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_agent_run(session_id, "slow", str(agent_dir / "request-ack1.json"), str(agent_dir / "status-ack1.json"))

            def fake_apply(_proposed, _context):
                self.assertEqual(bot.telegram.answers, ["Applying agent action(s)..."])
                return ActionResult(True, "slow action done")

            with mock.patch("jobhunter.app.apply_agent_action", side_effect=fake_apply):
                bot.handle_action(TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb"))

            self.assertEqual(bot.telegram.answers, ["Applying agent action(s)..."])

    def test_agent_apply_duplicate_click_is_blocked_while_in_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            session_id = "busy1"
            agent_dir = bot.config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "response-busy1.json").write_text(
                json.dumps(
                    {
                        "answer": "Apply slow action",
                        "proposed_actions": [
                            {"kind": "directive_edit", "summary": "Slow", "payload": {"directive": "Skip duplicates"}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_agent_run(session_id, "slow", str(agent_dir / "request-busy1.json"), str(agent_dir / "status-busy1.json"))
            first = TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb1", message_id=10)
            second = TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb2", message_id=10)
            clicked_again = []

            def fake_apply(_proposed, _context):
                if not clicked_again:
                    clicked_again.append(True)
                    bot.handle_action(second)
                return ActionResult(True, "slow action done")

            with mock.patch("jobhunter.app.apply_agent_action", side_effect=fake_apply):
                bot.handle_action(first)

            self.assertIn("Still applying - please wait.", bot.telegram.answers)
            self.assertEqual(len(bot.database.recent_agent_actions(10)), 1)

    def test_agent_apply_is_idempotent_for_duplicate_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
            bot.telegram = FakeTelegram()
            session_id = "dupe1"
            agent_dir = bot.config.workspace_dir / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "response-dupe1.json").write_text(
                json.dumps(
                    {
                        "answer": "Apply directive",
                        "proposed_actions": [
                            {"kind": "directive_edit", "summary": "Add directive", "payload": {"directive": "Skip chess jobs"}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bot.database.create_agent_run(session_id, "directive", str(agent_dir / "request-dupe1.json"), str(agent_dir / "status-dupe1.json"))

            action = TelegramAction(scope="agent", action="apply", target_id=session_id, callback_id="cb")
            bot.handle_action(action)
            bot.handle_action(action)

            rows = bot.database.recent_agent_actions(10)
            self.assertEqual(len(rows), 1)
            self.assertIn("already applied", bot.telegram.messages[-1][0])

    def test_discovery_approval_appends_test_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
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
            with mock.patch("jobhunter.app.validate_safe_url"):
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
            bot = JobHunter(config)
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
            with mock.patch("jobhunter.app.validate_safe_url"):
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
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config)
            bot.telegram = FakeTelegram()
            job_id = add_scored_job(bot)
            bot.generate_cover_note(job_id)
            self.assertEqual(bot.telegram.messages[0][0], "override")

    def test_cover_note_skips_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config_for(tmp))
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
            bot = JobHunter(config)
            bot.telegram = FakeTelegram()
            bot.handle_action(TelegramAction(scope="bot", action="discover_sources", callback_id="cb"))
            request_path = next((config.workspace_dir / "agent").glob("request-*.json"))
            request = request_path.read_text(encoding="utf-8")
            self.assertNotIn("Authorization", request)
            self.assertNotIn("secret", request)


if __name__ == "__main__":
    unittest.main()
