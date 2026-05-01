import tempfile
import unittest
from pathlib import Path

from jobbot.database import Database
from jobbot.models import Job, ScoreResult


class DatabaseTests(unittest.TestCase):
    def test_upsert_job_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            job = Job(
                source_id="test",
                source_name="Test",
                external_id="1",
                url="https://example.com/jobs/1",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            job_id, inserted = db.upsert_job(job)
            self.assertTrue(inserted)
            db.save_score(job_id, ScoreResult(score=88, hard_reject=False, reasons=["Good fit"], concerns=[]))
            rows = db.jobs_for_digest(10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["score"], 88)

    def test_cross_source_dedupe_and_no_respam(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            first = Job(
                source_id="remoteok",
                source_name="RemoteOK",
                external_id="1",
                url="https://example.com/jobs/1?utm_source=x#frag",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            second = Job(
                source_id="remotive",
                source_name="Remotive",
                external_id="2",
                url="https://example.com/jobs/1",
                title="Senior AI Engineer",
                company="ExampleCo",
            )
            job_id, inserted = db.upsert_job(first)
            self.assertTrue(inserted)
            same_id, inserted = db.upsert_job(second)
            self.assertEqual(job_id, same_id)
            self.assertFalse(inserted)
            db.save_score(job_id, ScoreResult(score=88, hard_reject=False, reasons=["Good fit"], concerns=[]))
            self.assertEqual(len(db.jobs_for_digest(10)), 1)
            db.mark_digested([job_id])
            self.assertEqual(len(db.jobs_for_digest(10)), 0)


if __name__ == "__main__":
    unittest.main()
