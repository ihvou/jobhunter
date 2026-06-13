import sqlite3
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
            "source-failure cleanup summaries",
            "recurring QA findings",
            "completed Engineer reports",
            "not like '%expected transient%'",
            "not like '%needs_clarification%'",
            "not like '%failure_reports%'",
            "not like '%failure-report%'",
            "not like '%failure report%'",
            "not like '%source-failure%'",
            "not like '%source failure%'",
            "not like '%recurring qa%'",
            "not like '%recurring finding%'",
            "agent = 'engineer'",
            "like '%pr_url%'",
            "like '%tests_passing%'",
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

    def test_failure_reports_query_suppresses_historical_noise(self):
        text = QA_SKILL.read_text()
        marker = "with candidate_reports as ("
        start = text.index(marker)
        end = text.index("limit 20;", start) + len("limit 20;")
        query = text[start:end]

        with sqlite3.connect(":memory:") as conn:
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
                    summary text,
                    payload_json text,
                    status text,
                    completed_at text
                );
                """
            )
            reports = [
                (1, "qa", "2026-06-13", "blocked collector handoff error", "{}", "2026-06-13 19:00:00"),
                (2, "qa", "2026-06-13", "failure_reports cleanup completed", "{}", "2026-06-13 19:01:00"),
                (3, "qa", "2026-06-13", "source-failure cleanup completed", "{}", "2026-06-13 19:02:00"),
                (4, "qa", "2026-06-13", "recurring QA finding: failure query returned old reports", "{}", "2026-06-13 19:03:00"),
                (5, "engineer", "2026-06-13", "completed failure bug", '{"pr_url":"https://example.test/pr/1","tests_passing":true}', "2026-06-13 19:04:00"),
            ]
            conn.executemany(
                "insert into agent_reports (id, agent, report_date, summary, details_json, created_at) values (?, ?, ?, ?, ?, ?)",
                reports,
            )

            rows = conn.execute(query).fetchall()
            self.assertEqual([row["id"] for row in rows], [1])

            conn.execute(
                """
                insert into agent_tasks (id, kind, summary, payload_json, status, completed_at)
                values (1, 'qa.bug', 'failure-report remediation', '{"anti_pattern": "failure_reports"}', 'open', null)
                """
            )
            self.assertEqual(conn.execute(query).fetchall(), [])

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
