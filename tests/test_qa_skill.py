import unittest
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QA_SKILL = ROOT / "skills" / "qa" / "SKILL.md"
QA_OBSERVABILITY_DESIGN = ROOT / "docs" / "qa" / "sensitive-observability-design.md"


class QASkillTests(unittest.TestCase):
    def _failure_reports_sql(self):
        text = QA_SKILL.read_text()
        section_start = text.index("5. Agent reports mention unresolved operational failures")
        sql_start = text.index("```sql", section_start) + len("```sql")
        sql_end = text.index("```", sql_start)
        return text[sql_start:sql_end].strip()

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
            "created_at >= datetime('now', '-36 hours')",
            "agent != 'qa'",
            "report_text like '%blocked%'",
            "from candidate_reports",
            "from actionable_reports",
            "active_failure_reports_task",
            "from agent_tasks",
            "kind = 'qa.bug'",
            "status in ('open', 'picked')",
            "completed_at >= datetime('now', '-7 days')",
            '%"anti_pattern": "failure_reports"%',
            "treat the",
            "reports as linked to that task",
            "not like '%expected transient%'",
            "not like '%needs_clarification%'",
            "not like '%historical%'",
            "not like '%prior qa%'",
            "not like '%failure_reports%'",
            "not like '%qa.bug%'",
            "not like '%risk%'",
            "trailing_avg >= 5",
            "last_24h < trailing_avg * 0.3",
        ]
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_failure_reports_query_filters_noise_and_dedupes(self):
        sql = self._failure_reports_sql()
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
              kind text,
              status text,
              completed_at text,
              summary text,
              payload_json text
            );
            insert into agent_reports (id, agent, report_date, summary, details_json, created_at)
            values
              (1, 'engineer', date('now'), 'Engineer blocked by gh auth failure', '{}', datetime('now', '-1 hour')),
              (2, 'qa', date('now'), 'QA failure_reports anti_pattern check found historical rows', '{}', datetime('now', '-1 hour')),
              (3, 'pm', date('now'), 'Historical risk: failed source trend mentioned in retrospective', '{}', datetime('now', '-1 hour')),
              (4, 'engineer', date('now'), 'Live smoke skipped', '{"note":"smoke skipped because JOBHUNTER_RUN_LIVE is unset"}', datetime('now', '-1 hour')),
              (5, 'collector', date('now'), 'Old collector failed run', '{}', datetime('now', '-3 days'));
            """
        )

        rows = conn.execute(sql).fetchall()
        self.assertEqual([row["id"] for row in rows], [1])

        conn.execute(
            """
            insert into agent_tasks (kind, status, completed_at, summary, payload_json)
            values ('qa.bug', 'open', null, 'Failure-report QA noise',
                    '{"anti_pattern": "failure_reports"}')
            """
        )
        rows = conn.execute(sql).fetchall()
        self.assertEqual(rows, [])

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
