import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from jobhunter.models import SourceConfig
from jobhunter.sources import collect_imap_alerts


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
        with tempfile.TemporaryDirectory() as tmp:
            sample_dir = Path(tmp) / "email_samples"
            env = {
                "EMAIL_IMAP_HOST": "imap.example.com",
                "EMAIL_IMAP_USERNAME": "user",
                "EMAIL_IMAP_PASSWORD": "password",
                "EMAIL_IMAP_FOLDER": "job-alerts",
                "JOBHUNTER_EMAIL_SAMPLES_DIR": str(sample_dir),
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
                raw_rows = []

                def raw_writer(source_id, message_id, sender, subject, received_at, raw_html, raw_text):
                    raw_rows.append(
                        {
                            "source_id": source_id,
                            "message_id": message_id,
                            "sender": sender,
                            "subject": subject,
                            "received_at": received_at,
                            "raw_html": raw_html,
                            "raw_text": raw_text,
                        }
                    )
                    return len(raw_rows), True

                djinni.raw_email_writer = raw_writer
                wellfound.raw_email_writer = raw_writer

                djinni_jobs = collect_imap_alerts(djinni)
                wellfound_jobs = collect_imap_alerts(wellfound)

                self.assertEqual(djinni_jobs, [])
                self.assertEqual(wellfound_jobs, [])
                self.assertEqual([row["source_id"] for row in raw_rows], ["djinni", "wellfound"])
                self.assertIn("Apply: https://djinni.co/jobs/1", raw_rows[0]["raw_text"])
                self.assertEqual(djinni.last_seen_uid, 1)
                self.assertEqual(wellfound.last_seen_uid, 2)
                # SEARCH includes UID range, the FROM filter, and (by default) a SINCE
                # cutoff inserted between them. Verify the essential clauses are present
                # rather than pinning the exact tuple — the SINCE date varies daily.
                matching = [s for s in mailbox.searches if s[1:4] == ("UID", "1:*", "SINCE") and "FROM" in s and '"no-reply@djinni.co"' in s]
                self.assertEqual(len(matching), 1)

                samples = sorted(sample_dir.glob("*/*.html"))
                self.assertEqual(len(samples), 2)
                self.assertIn("Apply: https://djinni.co/jobs/1", samples[0].read_text(encoding="utf-8") + samples[1].read_text(encoding="utf-8"))

                djinni.imap_last_uid = djinni.last_seen_uid
                self.assertEqual(collect_imap_alerts(djinni), [])


    def test_date_cutoff_applies_since_filter_to_search(self):
        """When JOBHUNTER_EMAIL_MAX_AGE_DAYS is set, the IMAP search must
        include a SINCE clause so old archived emails (e.g. 2022 alerts
        bulk-labeled in 2026) are filtered out server-side."""
        mailbox = FakeIMAP({})
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "EMAIL_IMAP_HOST": "imap.example.com",
                "EMAIL_IMAP_USERNAME": "user",
                "EMAIL_IMAP_PASSWORD": "password",
                "EMAIL_IMAP_FOLDER": "job-alerts",
                "JOBHUNTER_EMAIL_SAMPLES_DIR": str(Path(tmp) / "samples"),
                "JOBHUNTER_EMAIL_MAX_AGE_DAYS": "30",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "imaplib.IMAP4_SSL", return_value=mailbox
            ):
                src = SourceConfig(id="src", name="Src", type="imap", url="imap://job-alerts")
                src.raw_email_writer = lambda *args, **kwargs: (1, True)
                src.processed_uids_loader = lambda source_id: set()
                src.processed_uids_recorder = lambda source_id, uids: None
                collect_imap_alerts(src)

        # No source.query path: should issue exactly one SEARCH with SINCE.
        self.assertEqual(len(mailbox.searches), 1)
        args = mailbox.searches[0]
        self.assertIn("SINCE", args)
        since_idx = args.index("SINCE")
        # Date format DD-Mon-YYYY (IMAP SEARCH SINCE convention)
        import re
        self.assertRegex(args[since_idx + 1], r"^\d{2}-[A-Z][a-z]{2}-\d{4}$")

    def test_date_cutoff_disabled_when_zero(self):
        mailbox = FakeIMAP({})
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "EMAIL_IMAP_HOST": "imap.example.com",
                "EMAIL_IMAP_USERNAME": "user",
                "EMAIL_IMAP_PASSWORD": "password",
                "EMAIL_IMAP_FOLDER": "job-alerts",
                "JOBHUNTER_EMAIL_SAMPLES_DIR": str(Path(tmp) / "samples"),
                "JOBHUNTER_EMAIL_MAX_AGE_DAYS": "0",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "imaplib.IMAP4_SSL", return_value=mailbox
            ):
                src = SourceConfig(id="src", name="Src", type="imap", url="imap://job-alerts")
                src.raw_email_writer = lambda *args, **kwargs: (1, True)
                src.processed_uids_loader = lambda source_id: set()
                src.processed_uids_recorder = lambda source_id, uids: None
                collect_imap_alerts(src)

        self.assertEqual(len(mailbox.searches), 1)
        args = mailbox.searches[0]
        self.assertNotIn("SINCE", args)
        # Plain SEARCH ALL when no cutoff and no source.query
        self.assertIn("ALL", args)


if __name__ == "__main__":
    unittest.main()
