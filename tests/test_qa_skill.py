import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QA_SKILL = ROOT / "skills" / "qa" / "SKILL.md"
QA_OBSERVABILITY_DESIGN = ROOT / "docs" / "qa" / "sensitive-observability-design.md"


class QASkillTests(unittest.TestCase):
    def test_db_observable_antipatterns_are_explicit(self):
        text = QA_SKILL.read_text()
        expected_fragments = [
            "status = 'picked'",
            "picked_at < datetime('now', '-24 hours')",
            "from source_runs",
            "having failed_count >= 2",
            "lower(title) like '%apply for%'",
            "title like '%Відгукнутись%'",
            "from email_alert_raw",
            "parsed_at is null",
            "from agent_reports",
            "lower(summary) like '%stuck%'",
            "lower(summary) like '%blocked%'",
            "from candidate_reports",
            "from agent_tasks",
            "kind = 'qa.bug'",
            "status in ('open', 'picked')",
            "completed_at >= datetime('now', '-7 days')",
            '%"anti_pattern": "failure_reports"%',
            "treat the reports as linked to that task",
            "self-referential QA/report wording",
            "not like '%expected transient%'",
            "not like '%needs_clarification%'",
            "not like '%failure_reports%'",
            "not like '%failure-report%'",
            "not like '%failure report%'",
            "trailing_avg >= 5",
            "last_24h < trailing_avg * 0.3",
        ]
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_sensitive_observability_design_covers_security_contract(self):
        text = QA_OBSERVABILITY_DESIGN.read_text()
        expected_fragments = [
            "qa_read_gateway_logs",
            "qa_recent_chat_messages",
            "qa_read_agent_trajectory_summary",
            "qa_runtime_metrics",
            "[REDACTED:GITHUB_TOKEN]",
            "[REDACTED:TELEGRAM_BOT_TOKEN]",
            "content_is_untrusted",
            "QA-only",
            "Do not add Docker socket mounts",
            "no `exec` action kind",
        ]
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_bug_filing_payload_contract_is_documented(self):
        text = QA_SKILL.read_text()
        for fragment in [
            '"kind": "qa.bug"',
            '"anti_pattern": "stuck_picked_task"',
            '"sample_ids": [123]',
            '"observed"',
            '"expected"',
            '"repro_steps"',
        ]:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main()
