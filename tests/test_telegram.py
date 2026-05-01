import unittest

from jobbot.telegram import job_keyboard, parse_callback


class TelegramTests(unittest.TestCase):
    def test_parse_callback(self):
        action = parse_callback("job:cover_note:abc123")
        self.assertEqual(action.action, "cover_note")
        self.assertEqual(action.job_id, "abc123")

    def test_keyboard_contains_required_actions(self):
        keyboard = job_keyboard("abc123")
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(
            labels,
            ["Irrelevant", "Remind me tomorrow", "Give me cover note", "Applied"],
        )


if __name__ == "__main__":
    unittest.main()

