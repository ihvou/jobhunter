import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobhunter.agent_actions import AgentActionContext, apply_agent_action, sanitize_actions
from jobhunter.app import JobHunter
from jobhunter.config import split_profile_sections
from test_app import config_for


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
            bot = JobHunter(config)
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

    def test_directive_edit_rejects_payload_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nBuilder PM\n\n# Directives\n", encoding="utf-8")
            bot = JobHunter(config)
            context = AgentActionContext(config=config, database=bot.database, profile=bot.profile)

            result = apply_agent_action({"kind": "directive_edit", "payload": {"append": "X"}}, context)

            self.assertFalse(result.applied)
            self.assertIn("unknown payload key 'append'", result.message)
            self.assertIn("'directive'", result.message)

    def test_profile_edit_preserves_directives(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nOld\n\n# Directives\nKeep this\n", encoding="utf-8")
            bot = JobHunter(config)
            context = AgentActionContext(config=config, database=bot.database, profile=bot.profile)

            result = apply_agent_action(
                {"kind": "profile_edit", "payload": {"new_about_me": "New"}},
                context,
            )

            self.assertTrue(result.applied)
            sections = split_profile_sections(config.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(sections["about_me"], "New")
            self.assertEqual(sections["directives"], "Keep this")

    def test_profile_and_scoring_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nOld\n\n# Directives\n", encoding="utf-8")
            bot = JobHunter(config)
            context = AgentActionContext(config=config, database=bot.database, profile=bot.profile)

            profile_result = apply_agent_action({"kind": "profile_edit", "payload": {"about_me": "New"}}, context)
            scoring_result = apply_agent_action(
                {"kind": "scoring_rule_proposal", "payload": {"proposed_rules": {"rules": []}}},
                context,
            )

            self.assertFalse(profile_result.applied)
            self.assertIn("'new_about_me'", profile_result.message)
            self.assertFalse(scoring_result.applied)
            self.assertIn("'ruleset'", scoring_result.message)

    def test_sources_proposal_defaults_agent_sources_to_low_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            bot = JobHunter(config)
            context = AgentActionContext(
                config=config,
                database=bot.database,
                profile=bot.profile,
                source_reachable=lambda _url: True,
            )

            with mock.patch("jobhunter.agent_actions.validate_safe_url"):
                result = apply_agent_action(
                    {
                        "kind": "sources_proposal",
                        "payload": {
                            "operations": [
                                {
                                    "op": "add",
                                    "source": {
                                        "id": "agent-feed",
                                        "name": "Agent Feed",
                                        "type": "rss",
                                        "url": "https://example.com/jobs.rss",
                                    },
                                }
                            ]
                        },
                    },
                    context,
                )

            self.assertTrue(result.applied)
            sources = json.loads(config.sources_path.read_text(encoding="utf-8"))
            self.assertEqual(sources[0]["created_by"], "agent")
            self.assertEqual(sources[0]["risk_level"], "low")

    def test_recorded_service_action_apply_and_revert_audits_file_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = config_for(tmp)
            config.profile_path.write_text("# About me\n\nBuilder PM\n\n# Directives\n", encoding="utf-8")
            bot = JobHunter(config)
            from jobhunter.service import JobHunterService

            service = JobHunterService(bot)
            proposed = service.propose_actions(
                [
                    {
                        "kind": "directive_edit",
                        "summary": "Add exclusion",
                        "payload": {"directive": "Skip Product Marketing Manager roles"},
                    }
                ],
                user_intent="remember this",
                session_id="openclaw-test",
            )
            action_id = proposed["actions"][0]["id"]

            service.apply_action(action_id=action_id)
            rows = bot.database.recent_agent_actions(10)
            self.assertEqual(rows[0]["status"], "applied")
            self.assertIn("Skip Product Marketing", config.profile_path.read_text(encoding="utf-8"))

            service.revert_action(rows[0]["id"])
            self.assertNotIn("Skip Product Marketing", config.profile_path.read_text(encoding="utf-8"))
            self.assertEqual(bot.database.get_agent_action(rows[0]["id"])["status"], "reverted")


if __name__ == "__main__":
    unittest.main()
