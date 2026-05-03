import tempfile
import time
import unittest
from pathlib import Path

from jobhunter.database import Database
from jobhunter.models import Job, ScoreResult


def add_job(db: Database, suffix: str, score: int, title: str = None) -> str:
    job_id, _ = db.upsert_job(
        Job(
            source_id="behavior",
            source_name="Behavior",
            external_id=suffix,
            url="https://example.com/jobs/%s" % suffix,
            title=title or "AI Product Manager %s" % suffix,
            company="ExampleCo",
            description="Build AI workflows with agents and product teams.",
        )
    )
    db.save_score(job_id, ScoreResult(score=score, hard_reject=False))
    return job_id


class BehaviorTests(unittest.TestCase):
    def test_digest_falls_back_to_top_10_when_rules_threshold_would_hide_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            ids = [add_job(db, str(index), 20 + index) for index in range(12)]

            rows = db.jobs_for_digest(10, min_score=999)

            self.assertEqual(len(rows), 10)
            self.assertEqual(rows[0]["id"], ids[-1])

    def test_l2_not_relevant_is_visible_but_ranked_lower(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            good_id = add_job(db, "good", 80)
            bad_id = add_job(db, "bad", 80, "Product Marketing Manager")
            db.save_l2_verdict(good_id, "relevant", "high", "AI product builder role", [], "test")
            db.save_l2_verdict(bad_id, "not_relevant", "low", "Product marketing role", [], "test")

            rows = db.jobs_for_digest(10)

            self.assertEqual([row["id"] for row in rows[:2]], [good_id, bad_id])
            self.assertLess(rows[1]["total_score"], rows[0]["total_score"])

    def test_snoozed_due_jobs_surface_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            job_id = add_job(db, "snoozed", 90)
            db.mark_digested([job_id])
            db.update_job_status(job_id, "snoozed", snoozed_until="2000-01-01T00:00:00Z")

            rows = db.jobs_for_digest(10)

            self.assertEqual([row["id"] for row in rows], [job_id])

    def test_ranked_digest_query_handles_1000_jobs_quickly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "jobs.sqlite")
            db.init_schema()
            for index in range(1000):
                add_job(db, str(index), index % 100)

            started = time.time()
            rows = db.jobs_for_digest(10)

            self.assertEqual(len(rows), 10)
            self.assertLess(time.time() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
