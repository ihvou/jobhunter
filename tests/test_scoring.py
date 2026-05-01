import unittest

from jobbot.models import Job, UserProfile
from jobbot.scoring import score_job


class ScoringTests(unittest.TestCase):
    def test_scores_remote_skill_match(self):
        profile = UserProfile(
            raw_text="Python TypeScript LLM product engineer",
            target_titles=["ai engineer", "senior software engineer"],
            positive_keywords=["python", "typescript", "llm"],
            required_locations=["remote", "europe"],
        )
        job = Job(
            source_id="test",
            source_name="Test",
            external_id="1",
            url="https://example.com/job",
            title="Senior AI Engineer",
            company="ExampleCo",
            location="Remote Europe",
            remote_policy="remote",
            description="Build LLM systems with Python and TypeScript.",
        )
        result = score_job(job, profile)
        self.assertFalse(result.hard_reject)
        self.assertGreaterEqual(result.score, 70)
        self.assertTrue(result.reasons)

    def test_hard_rejects_excluded_domain(self):
        profile = UserProfile(
            raw_text="Engineer",
            target_titles=["engineer"],
            positive_keywords=["python"],
            excluded_domains=["gambling"],
        )
        job = Job(
            source_id="test",
            source_name="Test",
            external_id="1",
            url="https://example.com/job",
            title="Senior Engineer",
            company="ExampleCo",
            description="Build gambling products with Python.",
        )
        result = score_job(job, profile)
        self.assertTrue(result.hard_reject)
        self.assertLessEqual(result.score, 20)


if __name__ == "__main__":
    unittest.main()

