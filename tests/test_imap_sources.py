import os
import unittest
from email.message import EmailMessage
from unittest import mock

from jobbot.models import SourceConfig
from jobbot.sources import collect_imap_alerts


def make_message(subject, sender, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Message-ID"] = "<%s>" % subject.lower().replace(" ", "-")
    message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.searches = []
        self.closed = False
        self.logged_out = False

    def login(self, _username, _password):
        return "OK", []

    def select(self, _folder, readonly=True):
        return "OK", []

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            return "OK", [self.search(args)]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(b"RFC822", self.messages[uid][1])]
        return "NO", []

    def search(self, args):
        uid_start = 1
        sender_filter = ""
        for idx, value in enumerate(args):
            if value == "UID" and idx + 1 < len(args):
                uid_start = int(str(args[idx + 1]).split(":", 1)[0])
            if value == "FROM" and idx + 1 < len(args):
                sender_filter = str(args[idx + 1]).strip('"').lower()
        matches = []
        for uid, (sender, _payload) in self.messages.items():
            if uid < uid_start:
                continue
            if sender_filter and sender_filter not in sender.lower():
                continue
            matches.append(str(uid).encode("ascii"))
        return b" ".join(matches)

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


class ImapSourceTests(unittest.TestCase):
    def test_per_source_query_and_uid_progress(self):
        messages = {
            1: (
                "no-reply@djinni.co",
                make_message("Djinni Product Manager", "no-reply@djinni.co", "Apply: https://djinni.co/jobs/1"),
            ),
            2: (
                "alerts@wellfound.com",
                make_message("Wellfound AI Engineer", "alerts@wellfound.com", "Apply: https://wellfound.com/jobs/2"),
            ),
        }
        mailbox = FakeIMAP(messages)
        env = {
            "EMAIL_IMAP_HOST": "imap.example.com",
            "EMAIL_IMAP_USERNAME": "user",
            "EMAIL_IMAP_PASSWORD": "password",
            "EMAIL_IMAP_FOLDER": "job-alerts",
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("imaplib.IMAP4_SSL", return_value=mailbox):
            djinni = SourceConfig(
                id="djinni",
                name="Djinni",
                type="imap",
                url="imap://job-alerts",
                query='FROM "no-reply@djinni.co"',
            )
            wellfound = SourceConfig(
                id="wellfound",
                name="Wellfound",
                type="imap",
                url="imap://job-alerts",
                query='FROM "alerts@wellfound.com"',
            )

            djinni_jobs = collect_imap_alerts(djinni)
            wellfound_jobs = collect_imap_alerts(wellfound)

            self.assertEqual([job.url for job in djinni_jobs], ["https://djinni.co/jobs/1"])
            self.assertEqual([job.url for job in wellfound_jobs], ["https://wellfound.com/jobs/2"])
            self.assertEqual(djinni.last_seen_uid, 1)
            self.assertEqual(wellfound.last_seen_uid, 2)
            self.assertIn((None, "UID", "1:*", "FROM", '"no-reply@djinni.co"'), mailbox.searches)

            djinni.imap_last_uid = djinni.last_seen_uid
            self.assertEqual(collect_imap_alerts(djinni), [])


if __name__ == "__main__":
    unittest.main()
