import unittest

from jobbot.models import Job, UserProfile
from jobbot.scoring import score_job, word_boundary_search


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

    def test_scoring_rule_kinds(self):
        profile = UserProfile(raw_text="", positive_keywords=["llm"])
        job = Job(
            source_id="test",
            source_name="Test",
            external_id="1",
            url="https://example.com/job",
            title="Senior Product Engineer",
            company="ExampleCo",
            remote_policy="remote",
            salary_max=120000,
            description="Build LLM automation prototypes with Python.",
        )
        rules = {
            "rules": [
                {"id": "any", "kind": "match_any_word", "fields": ["title"], "patterns": ["product engineer"], "weight": 10},
                {"id": "all", "kind": "match_all_word", "fields": ["description"], "patterns": ["llm", "python"], "weight": 10},
                {"id": "eq", "kind": "field_equals", "field": "remote_policy", "value": "remote", "weight": 10},
                {"id": "num", "kind": "numeric_at_least", "field": "salary_max", "threshold": 100000, "weight": 10},
                {"id": "sim", "kind": "feedback_similarity", "fields": ["description"], "patterns": ["llm"], "min_hits": 1, "weight": 10},
                {"id": "reject", "kind": "hard_reject_word", "fields": ["description"], "patterns": ["intern"]},
            ],
            "thresholds": {"hard_reject_floor": 0},
        }
        result = score_job(job, profile, rules)
        self.assertFalse(result.hard_reject)
        self.assertEqual(result.score, 50)
        self.assertIn("any", result.breakdown)

    def test_word_boundary_matching_avoids_false_rejects(self):
        self.assertFalse(word_boundary_search("intern", "international product role"))
        self.assertFalse(word_boundary_search("us only", "trust only matters"))
        self.assertFalse(word_boundary_search("weapons", "anti-weapons policy analyst"))


if __name__ == "__main__":
    unittest.main()
