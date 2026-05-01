import unittest

from jobbot.telegram import job_keyboard, parse_callback


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

    def test_keyboard_contains_required_actions(self):
        keyboard = job_keyboard("abc123")
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(
            labels,
            ["Irrelevant", "Remind me tomorrow", "Give me cover note", "Applied"],
        )


if __name__ == "__main__":
    unittest.main()
