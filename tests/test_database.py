import tempfile
import unittest
from pathlib import Path

from jobhunter.database import Database
from jobhunter.models import Job, ScoreResult


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
            self.assertEqual(rows[0]["score"], 44)
            self.assertEqual(rows[0]["l1_score"], 44)

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

    def test_digest_ignores_score_threshold_and_sorts_by_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            low_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="1", url="https://example.com/low", title="Low", company="C")
            )
            high_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="2", url="https://example.com/high", title="High", company="C")
            )
            db.save_score(low_id, ScoreResult(score=30, hard_reject=False))
            db.save_score(high_id, ScoreResult(score=80, hard_reject=False))
            rows = db.jobs_for_digest(10, min_score=50)
            self.assertEqual([row["id"] for row in rows], [high_id, low_id])

    def test_due_snoozed_jobs_sort_by_total_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            snoozed_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="snoozed", url="https://example.com/snoozed", title="Snoozed", company="C")
            )
            fresh_id, _ = db.upsert_job(
                Job(source_id="s", source_name="S", external_id="fresh", url="https://example.com/fresh", title="Fresh", company="C")
            )
            db.save_score(snoozed_id, ScoreResult(score=95, hard_reject=False))
            db.save_score(fresh_id, ScoreResult(score=70, hard_reject=False))
            db.update_job_status(snoozed_id, "snoozed", snoozed_until="2000-01-01T00:00:00Z")

            rows = db.jobs_for_digest(10)

            self.assertEqual([row["id"] for row in rows], [snoozed_id, fresh_id])

    def test_secondary_dedupe_same_title_company_nearby_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            first_id, inserted = db.upsert_job(
                Job(
                    source_id="wwr",
                    source_name="WWR",
                    external_id="1",
                    url="https://example.com/userwise-services-product-manager",
                    title="Product Manager",
                    company="Userwise Services",
                    posted_at="2026-05-01T00:00:00Z",
                )
            )
            self.assertTrue(inserted)
            second_id, inserted = db.upsert_job(
                Job(
                    source_id="wwr",
                    source_name="WWR",
                    external_id="2",
                    url="https://example.com/userwise-services-product-manager-1",
                    title="Product Manager",
                    company="Userwise Services",
                    posted_at="2026-05-03T00:00:00Z",
                )
            )
            self.assertEqual(first_id, second_id)
            self.assertFalse(inserted)


if __name__ == "__main__":
    unittest.main()
