import unittest
import sqlite3
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
            "completed Engineer reports",
            "summary-first",
            "agent != 'qa'",
            "not like '%status report%'",
            "not like '%routine completed%'",
            "not like '%\"failed_count\": 0%'",
            "not like '%\"blocked_count\": 0%'",
            "not like '%\"error_count\": 0%'",
            "not like '%\"errors\": 0%'",
            "not like '%\"blocked\": false%'",
            'like \'%"status": "failed"%\'',
            'like \'%"status":"blocked"%\'',
            "not like '%expected transient%'",
            "not like '%needs_clarification%'",
            "not like '%failure_reports%'",
            "not like '%failure-report%'",
            "not like '%failure report%'",
            "agent = 'engineer'",
            "like '%pr_url%'",
            "like '%tests_passing%'",
            "trailing_avg >= 5",
            "last_24h < trailing_avg * 0.3",
        ]
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_failure_reports_query_ignores_routine_status_and_prior_qa_reports(self):
        text = QA_SKILL.read_text()
        start = text.index("with candidate_reports as (")
        end = text.index("limit 20;", start) + len("limit 20;")
        sql = text[start:end]

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            create table agent_reports (
              id integer primary key,
              agent text,
              report_date text,
              summary text,
              details_json text,
              created_at text
            );
            create table agent_tasks (
              id integer primary key,
              kind text,
              status text,
              completed_at text,
              summary text,
              payload_json text
            );
            """
        )
        rows = [
            (
                1,
                "qa",
                "2026-06-14",
                "QA noted failure_reports noise for follow-up",
                "{}",
            ),
            (
                2,
                "collector",
                "2026-06-14",
                "Collector status report",
                '{"failed_count": 0, "blocked_count": 0, "errors": 0}',
            ),
            (
                3,
                "researcher",
                "2026-06-14",
                "Researcher routine completed",
                '{"blocked": false, "error_count": 0}',
            ),
            (
                4,
                "collector",
                "2026-06-14",
                "Collector failed to parse source batch",
                '{"source": "example"}',
            ),
        ]
        conn.executemany(
            """
            insert into agent_reports
              (id, agent, report_date, summary, details_json, created_at)
            values (?, ?, ?, ?, ?, datetime('now', '-1 hour'))
            """,
            rows,
        )

        returned = conn.execute(sql).fetchall()
        self.assertEqual([row["id"] for row in returned], [4])

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
