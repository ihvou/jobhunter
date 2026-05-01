import unittest

from jobbot.telegram import TelegramClient, job_keyboard, main_menu_keyboard, parse_callback, parse_message


class TelegramTests(unittest.TestCase):
    def test_parse_callback(self):
        action = parse_callback("job:cover_note:abc123")
        self.assertEqual(action.action, "cover_note")
        self.assertEqual(action.job_id, "abc123")

    def test_parse_bot_and_approval_callbacks(self):
        self.assertEqual(parse_callback("bot:collect").scope, "bot")
        self.assertEqual(parse_callback("bot:discover_sources").action, "discover_sources")
        self.assertEqual(parse_callback("bot:tune_scoring").action, "tune_scoring")
        self.assertEqual(parse_callback("bot:usage").action, "usage")
        disc = parse_callback("disc:approve:session1:2")
        self.assertEqual(disc.scope, "disc")
        self.assertEqual(disc.index, 2)
        self.assertEqual(parse_callback("disc:reject:session1").action, "reject")
        self.assertEqual(parse_callback("tune:apply:session1").scope, "tune")
        self.assertEqual(parse_callback("tune:reject:session1").action, "reject")
        self.assertEqual(parse_callback("cover:override:job1").scope, "cover")

    def test_parse_reply_keyboard_messages_and_commands(self):
        cases = {
            "Get more jobs": "collect",
            "Update sources": "discover_sources",
            "Tune scoring": "tune_scoring",
            "Usage": "usage",
            "/jobs": "collect",
            "/jobs@jobhunter_bot": "collect",
            "/sources": "discover_sources",
            "/tune": "tune_scoring",
            "/usage": "usage",
            "/start": "menu",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_message(text).action, expected)
        self.assertIsNone(parse_message("hello"))

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


if __name__ == "__main__":
    unittest.main()
