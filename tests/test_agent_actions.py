import json
import tempfile
import unittest
from pathlib import Path

from jobbot.agent_actions import AgentActionContext, apply_agent_action, sanitize_actions
from jobbot.app import JobBot
from jobbot.config import split_profile_sections
from test_app import FakeTelegram, config_for


class AgentActionTests(unittest.TestCase):
    def test_unknown_action_kind_is_dropped(self):
        actions = sanitize_actions(
            [
                {"kind": "execute_python", "summary": "bad", "payload": {}},
                {"kind": "data_answer", "summary": "ok", "payload": {"answer": "hi"}},
            ]
        )
        self.assertEqual([action["kind"] for action in actions], ["data_answer"])

    def test_directive_edit_preserves_about_me_and_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nBuilder PM\n\n# Directives\n", encoding="utf-8")
            bot = JobBot(config)
            context = AgentActionContext(config=config, database=bot.database, profile=bot.profile)

            result = apply_agent_action(
                {"kind": "directive_edit", "payload": {"directive": "Skip Product Marketing Manager roles"}},
                context,
            )

            self.assertTrue(result.applied)
            self.assertTrue(Path(result.archive_path).exists())
            sections = split_profile_sections(config.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(sections["about_me"], "Builder PM")
            self.assertIn("Skip Product Marketing Manager roles", sections["directives"])

    def test_profile_edit_preserves_directives(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nOld\n\n# Directives\nKeep this\n", encoding="utf-8")
            bot = JobBot(config)
            context = AgentActionContext(config=config, database=bot.database, profile=bot.profile)

            result = apply_agent_action(
                {"kind": "profile_edit", "payload": {"new_about_me": "New"}},
                context,
            )

            self.assertTrue(result.applied)
            sections = split_profile_sections(config.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(sections["about_me"], "New")
            self.assertEqual(sections["directives"], "Keep this")

    def test_agent_response_apply_and_revert_audits_file_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nBuilder PM\n\n# Directives\n", encoding="utf-8")
            bot = JobBot(config)
            bot.telegram = FakeTelegram()
            session_id = bot.agent.create_request("remember this")
            response_path = config.workspace_dir / "agent" / ("response-%s.json" % session_id)
            response_path.write_text(
                json.dumps(
                    {
                        "user_intent_summary": "add directive",
                        "answer": "I can add it.",
                        "proposed_actions": [
                            {
                                "kind": "directive_edit",
                                "summary": "Add exclusion",
                                "payload": {"directive": "Skip Product Marketing Manager roles"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            status_path = config.workspace_dir / "agent" / ("status-%s.json" % session_id)
            status_path.write_text('{"state":"done","message":"ok"}', encoding="utf-8")
            bot.poll_workspace()

            bot.handle_action(type("Action", (), {"scope": "agent", "action": "apply", "target_id": session_id, "index": 0, "callback_id": "cb", "message_id": 1})())
            rows = bot.database.recent_agent_actions(10)
            self.assertEqual(rows[0]["status"], "applied")
            self.assertIn("Skip Product Marketing", config.profile_path.read_text(encoding="utf-8"))

            bot.handle_action(type("Action", (), {"scope": "bot", "action": "revert", "target_id": str(rows[0]["id"]), "callback_id": None})())
            self.assertNotIn("Skip Product Marketing", config.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(bot.database.get_agent_action(rows[0]["id"])["status"], "reverted")


if __name__ == "__main__":
    unittest.main()
