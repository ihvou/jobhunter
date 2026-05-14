import json
import tempfile
import unittest
from pathlib import Path

from jobhunter.app import JobHunter
from jobhunter.models import Job, ScoreResult, SourceConfig
from jobhunter.openclaw_mcp import handle_rpc
from jobhunter.service import JobHunterService, ServiceError
from test_app import config_for


ROOT = Path(__file__).resolve().parent.parent


class ServiceTests(unittest.TestCase):
    def seeded_bot(self, tmp):
        config = config_for(tmp)
        config.profile_path.write_text((ROOT / "input" / "profile.example.md").read_text(encoding="utf-8"), encoding="utf-8")
        bot = JobHunter(config)
        bot.initialize()
        bot.database.upsert_sources([SourceConfig(id="s", name="Source", type="rss", url="https://example.com/rss")])
        job_id, _ = bot.database.upsert_job(
            Job(
                source_id="s",
                source_name="Source",
                external_id="1",
                url="https://example.com/job",
                title="AI Product Manager",
                company="ExampleCo",
                description="Build AI agent workflows with product teams.",
            )
        )
        bot.database.save_score(job_id, ScoreResult(score=80, hard_reject=False, reasons=["AI product"], fired_rules=["title"]))
        return bot, job_id

    def test_digest_and_job_actions_are_exposed_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            digest = service.digest(limit=1)
            self.assertEqual(digest["count"], 1)
            self.assertEqual(digest["jobs"][0]["id"], job_id)
            self.assertEqual(digest["jobs"][0]["title"], "AI Product Manager")

            applied = service.mark_applied(job_id)
            self.assertTrue(applied["ok"])
            self.assertEqual(bot.database.get_job(job_id)["status"], "applied")
            action = bot.database.recent_agent_actions(1)[0]
            self.assertEqual(action["kind"], "mark_job")
            self.assertEqual(action["status"], "applied")
            payload = json.loads(action["payload_json"])
            self.assertEqual(payload["job_id"], job_id)
            self.assertEqual(payload["status"], "applied")

    def test_resolve_job_prefix_and_snooze_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            resolved = service.resolve_job_prefix(job_id[:12])
            self.assertEqual(resolved["job_id"], job_id)

            with self.assertRaises(ServiceError) as raised:
                service.resolve_job_prefix("not-a-prefix")
            self.assertEqual(raised.exception.status, 400)

            snoozed = service.snooze(job_id)
            self.assertTrue(snoozed["ok"])
            job = bot.database.get_job(job_id)
            self.assertEqual(job["status"], "snoozed")
            self.assertTrue(job["snoozed_until"])
            action = bot.database.recent_agent_actions(1)[0]
            self.assertEqual(action["kind"], "mark_job")
            self.assertEqual(json.loads(action["payload_json"])["job_id"], job_id)

    def test_query_sql_is_select_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)

            result = service.query_sql("select title from jobs", limit=5)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["rows"][0]["title"], "AI Product Manager")

            with self.assertRaises(ServiceError) as raised:
                service.query_sql("delete from jobs")
            self.assertEqual(raised.exception.status, 400)

    def test_propose_apply_and_revert_agent_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            before = bot.config.profile_path.read_text(encoding="utf-8")

            proposed = service.propose_actions(
                [
                    {
                        "kind": "directive_edit",
                        "summary": "Prefer AI builder roles",
                        "payload": {"directive": "Prioritize product roles building with Codex or Claude."},
                    }
                ],
                user_intent="tighten scoring",
                session_id="test-session",
            )
            action_id = proposed["actions"][0]["id"]

            applied = service.apply_action(action_id=action_id)
            self.assertTrue(applied["ok"])
            self.assertEqual(bot.database.get_agent_action(action_id)["status"], "applied")
            self.assertIn("Prioritize product roles", bot.config.profile_path.read_text(encoding="utf-8"))

            reverted = service.revert_action(action_id)
            self.assertTrue(reverted["ok"])
            self.assertEqual(bot.config.profile_path.read_text(encoding="utf-8"), before)

    def test_mcp_lists_and_calls_service_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            import jobhunter.openclaw_mcp as mcp

            old_get = mcp.get
            old_post = mcp.post

            def fake_get(path):
                if path.startswith("/usage"):
                    return service.usage()
                if path.startswith("/history"):
                    return service.history()
                raise AssertionError("unexpected GET %s" % path)

            def fake_post(path, payload):
                if path == "/digest":
                    return service.digest(limit=payload.get("limit"), mark_sent=bool(payload.get("mark_sent", False)))
                if path == "/query-sql":
                    return service.query_sql(payload.get("sql"), payload.get("params") or [], payload.get("limit") or 50)
                raise AssertionError("unexpected POST %s" % path)

            mcp.get = fake_get
            mcp.post = fake_post
            self.addCleanup(setattr, mcp, "get", old_get)
            self.addCleanup(setattr, mcp, "post", old_post)

            listed = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            self.assertIn("jobhunter_get_more_jobs", [tool["name"] for tool in listed["result"]["tools"]])
            self.assertNotIn("jobhunter_agent_request", [tool["name"] for tool in listed["result"]["tools"]])
            digest_tool = next(tool for tool in listed["result"]["tools"] if tool["name"] == "jobhunter_get_more_jobs")
            self.assertIn("read-only diagnostics", digest_tool["description"])
            self.assertIn("presentation", digest_tool["description"])
            self.assertNotIn("MANDATORY RENDERING CONTRACT", digest_tool["description"])

            called = handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "jobhunter_get_more_jobs", "arguments": {"limit": 1}},
                }
            )
            text = called["result"]["content"][0]["text"]
            self.assertIn("AI Product Manager", text)

    def test_mcp_mark_job_and_cover_note_accept_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, job_id = self.seeded_bot(tmp)
            service = JobHunterService(bot)
            import jobhunter.openclaw_mcp as mcp

            old_post = mcp.post
            calls = []

            def fake_post(path, payload):
                calls.append((path, payload))
                if path == "/jobs/resolve_prefix":
                    return service.resolve_job_prefix(payload["id_prefix"])
                if path == "/applied":
                    return service.mark_applied(payload["job_id"])
                if path == "/cover-note":
                    return {"ok": True, "job_id": payload["job_id"], "draft": "Cover draft"}
                raise AssertionError("unexpected POST %s" % path)

            mcp.post = fake_post
            self.addCleanup(setattr, mcp, "post", old_post)

            marked = handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "jobhunter_mark_job",
                        "arguments": {"id_prefix": job_id[:12], "status": "applied"},
                    },
                }
            )
            marked_payload = json.loads(marked["result"]["content"][0]["text"])
            self.assertEqual(marked_payload["status"], "applied")
            self.assertEqual(bot.database.get_job(job_id)["status"], "applied")

            cover = handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "jobhunter_cover_note",
                        "arguments": {"id_prefix": job_id[:12]},
                    },
                }
            )
            cover_payload = json.loads(cover["result"]["content"][0]["text"])
            self.assertEqual(cover_payload["draft"], "Cover draft")
            self.assertIn(("/cover-note", {"job_id": job_id, "override_budget": False}), calls)


if __name__ == "__main__":
    unittest.main()
