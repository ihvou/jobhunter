import unittest
from unittest import mock

from jobhunter.telegram import TelegramClient, TelegramError, agent_actions_keyboard, job_keyboard, main_menu_keyboard, parse_callback, parse_message, revert_keyboard


class TelegramTests(unittest.TestCase):
    def capture_agent_response(self, response):
        client = TelegramClient("", None)
        sent = []

        def fake_send(text, reply_markup=None, parse_mode=None):
            sent.append((text, reply_markup, parse_mode))

        client.send_message = fake_send
        client.send_agent_response("s1", response)
        return sent[-1]

    def test_parse_callback(self):
        action = parse_callback("job:cover_note:abc123")
        self.assertEqual(action.action, "cover_note")
        self.assertEqual(action.job_id, "abc123")

    def test_parse_bot_and_approval_callbacks(self):
        self.assertEqual(parse_callback("bot:collect").scope, "bot")
        self.assertEqual(parse_callback("bot:discover_sources").action, "discover_sources")
        self.assertEqual(parse_callback("bot:tune_scoring").action, "tune_scoring")
        self.assertEqual(parse_callback("bot:usage").action, "usage")
        revert = parse_callback("bot:revert:12")
        self.assertEqual(revert.action, "revert")
        self.assertEqual(revert.target_id, "12")
        disc = parse_callback("disc:approve:session1:2")
        self.assertEqual(disc.scope, "disc")
        self.assertEqual(disc.index, 2)
        self.assertEqual(parse_callback("disc:reject:session1").action, "reject")
        self.assertEqual(parse_callback("tune:apply:session1").scope, "tune")
        self.assertEqual(parse_callback("tune:reject:session1").action, "reject")
        self.assertEqual(parse_callback("cover:override:job1").scope, "cover")
        agent = parse_callback("agent:apply:session1:2")
        self.assertEqual(agent.scope, "agent")
        self.assertEqual(agent.index, 2)

    def test_parse_reply_keyboard_messages_and_commands(self):
        cases = {
            "Get more jobs": "collect",
            "Update sources": "discover_sources",
            "Tune scoring": "tune_scoring",
            "Usage": "usage",
            "/jobs": "collect",
            "/jobs@jobhunter_bot": "collect",
            "/refresh": "refresh_collect",
            "/sources": "discover_sources",
            "/tune": "tune_scoring",
            "/usage": "usage",
            "/start": "menu",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_message(text).action, expected)
        self.assertEqual(parse_message("hello").action, "agent")
        self.assertEqual(parse_message("hello").text, "hello")

    def test_parse_agent_commands(self):
        action = parse_message("/agent why did you miss this URL?")
        self.assertEqual(action.scope, "bot")
        self.assertEqual(action.action, "agent")
        self.assertIn("miss this URL", action.text)
        feedback = parse_message("/feedback skip German jobs")
        self.assertEqual(feedback.action, "agent")
        self.assertEqual(feedback.text, "/feedback skip German jobs")
        revert = parse_message("/revert 12")
        self.assertEqual(revert.action, "revert")
        self.assertEqual(revert.target_id, "12")
        confirm = parse_message("CONFIRM #34")
        self.assertEqual(confirm.action, "confirm")
        self.assertEqual(confirm.target_id, "34")

    def test_agent_keyboard_skips_data_answer_buttons(self):
        keyboard = agent_actions_keyboard(
            "s1",
            [
                {"kind": "data_answer", "summary": "read only"},
                {"kind": "directive_edit", "summary": "write"},
            ],
        )
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertIn("Apply 2", labels)
        self.assertNotIn("Apply 1", labels)

    def test_agent_response_renders_data_answer_inline_with_write_action(self):
        text, keyboard, _parse_mode = self.capture_agent_response(
            {
                "answer": "Top-level answer",
                "proposed_actions": [
                    {
                        "kind": "data_answer",
                        "summary": "Detailed rows",
                        "payload": {
                            "answer": "Detailed table with 3 rows",
                            "rows": [{"label": "one", "value": "1"}],
                        },
                    },
                    {"kind": "directive_edit", "summary": "Add directive", "payload": {"directive": "X"}},
                ],
            }
        )
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        numeric_apply = [label for label in labels if label.startswith("Apply ") and label[6:].isdigit()]
        self.assertIn("Top-level answer", text)
        self.assertIn("Detailed table with 3 rows", text)
        self.assertIn("| one | 1 |", text)
        self.assertEqual(numeric_apply, ["Apply 2"])
        self.assertNotIn("Apply 1", labels)

    def test_agent_response_with_only_data_answers_uses_main_menu(self):
        text, keyboard, _parse_mode = self.capture_agent_response(
            {
                "answer": "Here is the data.",
                "proposed_actions": [
                    {"kind": "data_answer", "summary": "Rows", "payload": {"answer": "No writes"}}
                ],
            }
        )
        self.assertIn("No writes", text)
        self.assertIn("keyboard", keyboard)
        self.assertNotIn("inline_keyboard", keyboard)

    def test_agent_response_renders_evidence_table(self):
        text, _keyboard, _parse_mode = self.capture_agent_response(
            {
                "answer": "Evidence found.",
                "evidence_table": [{"label": "a", "value": "1"}, {"label": "b", "value": "2"}],
                "proposed_actions": [],
            }
        )
        self.assertIn("| a | 1 |", text)
        self.assertIn("| b | 2 |", text)

    def test_main_menu_is_reply_keyboard(self):
        keyboard = main_menu_keyboard()
        self.assertIn("keyboard", keyboard)
        self.assertNotIn("inline_keyboard", keyboard)
        labels = [button["text"] for row in keyboard["keyboard"] for button in row]
        self.assertEqual(labels, ["Get more jobs", "Update sources", "Tune scoring", "Usage"])

    def test_keyboard_contains_required_actions(self):
        keyboard = job_keyboard("abc123")
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(
            labels,
            ["Irrelevant", "Remind me tomorrow", "Give me cover note", "Applied"],
        )
        self.assertEqual(revert_keyboard(12)["inline_keyboard"][0][0]["callback_data"], "bot:revert:12")

    def test_poll_actions_accepts_reply_keyboard_messages(self):
        client = TelegramClient("token", 123)
        client._get = lambda _query: {
            "result": [
                {
                    "update_id": 7,
                    "message": {
                        "message_id": 10,
                        "chat": {"id": 123},
                        "text": "Usage",
                    },
                }
            ]
        }

        actions = client.poll_actions()

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].scope, "bot")
        self.assertEqual(actions[0].action, "usage")
        self.assertEqual(actions[0].chat_id, 123)
        self.assertEqual(client.offset, 8)

    def test_urlopen_timeout_is_wrapped_as_telegram_error(self):
        client = TelegramClient("token", 123)
        with mock.patch("jobhunter.telegram.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(TelegramError, "timed out"):
                client._get("getUpdates")


if __name__ == "__main__":
    unittest.main()
