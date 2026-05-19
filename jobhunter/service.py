import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

from .agent_actions import ActionResult, AgentActionContext, apply_agent_action, sanitize_actions
from .app import JobHunter
from .config import load_app_config, load_sources
from .database import tomorrow_iso
from .logging_setup import configure_logging, log_context, safe_log_text
from .models import Lead
from .sources import SourceError, validate_safe_url

LOGGER = logging.getLogger(__name__)
JOB_ID_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")
LEAD_ID_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")
LEAD_STATUS_VALUES = {"new", "shortlisted", "reached_out", "rejected", "snoozed", "pitched", "archived"}


class JobHunterService:
    def __init__(self, bot: JobHunter):
        self.bot = bot

    @classmethod
    def from_environment(cls):
        bot = JobHunter(load_app_config())
        bot.initialize()
        return cls(bot)

    def health(self) -> Dict:
        return {"ok": True, "service": "jobhunter-service", "ts": time.time()}

    def usage(self) -> Dict:
        return self.bot.database.usage_summary()

    def show_profile(self) -> Dict:
        self.bot.refresh_profile()
        profile = self.bot.profile
        return {
            "ok": True,
            "path": str(self.bot.config.profile_path),
            "text": profile.raw_text,
            "about_me": profile.about_me,
            "directives": profile.directives,
            "cv_present": bool(profile.cv_text.strip()),
        }

    def show_icp(self) -> Dict:
        text = read_text_if_exists(self.bot.config.icp_path)
        return {
            "ok": True,
            "path": str(self.bot.config.icp_path),
            "text": text,
            "exists": self.bot.config.icp_path.exists(),
        }

    def history(self, limit: int = 10) -> Dict:
        return {"actions": [row_to_dict(row) for row in self.bot.database.recent_agent_actions(limit)]}

    def collect(self) -> Dict:
        before = self.count_jobs()
        self.bot.collect()
        after = self.count_jobs()
        return {"ok": True, "jobs_before": before, "jobs_after": after, "inserted_estimate": max(0, after - before)}

    def rescore_recent_jobs(self, limit: int = 500) -> Dict:
        limit = min(max(1, limit or 500), 1000)
        self.bot.rescore_recent_jobs(limit)
        return {"ok": True, "rescored_limit": limit}

    def digest(self, limit: int = None, mark_sent: bool = False) -> Dict:
        limit = limit or self.bot.config.digest_max_jobs
        rows = self.bot.database.jobs_for_digest(limit)
        jobs = [job_digest_row(row) for row in rows]
        digest_id = ""
        if mark_sent and jobs:
            digest_id = self.bot.database.mark_digested([job["id"] for job in jobs])
        payload = {"jobs": jobs, "count": len(jobs), "digest_id": digest_id, "marked_sent": bool(digest_id)}
        payload.update(self.bot.collection_freshness())
        return payload

    def mark_irrelevant(self, job_id: str, details: str = "") -> Dict:
        return self.mark_job(job_id, "rejected", "irrelevant", details)

    def mark_applied(self, job_id: str, details: str = "") -> Dict:
        result = self.mark_job(job_id, "applied", "applied", details)
        job = self.bot.database.get_job(job_id)
        if job:
            self.bot.database.promote_source_if_test(job["source_id"])
        return result

    def snooze(self, job_id: str) -> Dict:
        self.ensure_job(job_id)
        self.bot.database.update_job_status(job_id, "snoozed", snoozed_until=tomorrow_iso())
        self.bot.database.add_feedback(job_id, "snooze_1d")
        self.audit_mark_job(job_id, "snoozed", "snooze_1d")
        return {"ok": True, "job_id": job_id, "status": "snoozed"}

    def cover_note(self, job_id: str, override_budget: bool = False) -> Dict:
        job = self.ensure_job(job_id)
        draft = self.bot.llm.cover_note(self.bot.profile, job, override_budget=override_budget)
        self.bot.database.add_feedback(job_id, "cover_note")
        self.bot.database.save_draft(job_id, "cover_note", draft)
        self.bot.database.update_job_status(job_id, "draft_ready")
        return {"ok": True, "job_id": job_id, "draft": draft}

    def propose_actions(self, actions: List[Dict], user_intent: str = "", session_id: str = "") -> Dict:
        session_id = session_id or "openclaw-%s" % int(time.time() * 1000)
        sanitized = sanitize_actions(actions or [])
        proposed = []
        skipped = []
        for action in sanitized:
            if action.get("kind") == "data_answer":
                skipped.append({"kind": "data_answer", "reason": "read-only answers are not stored as actions"})
                continue
            existing = self.find_existing_action(session_id, action.get("kind", ""), action.get("payload", {}))
            if existing:
                proposed.append({"id": existing["id"], "kind": existing["kind"], "status": existing["status"], "summary": existing["summary"]})
                continue
            action_id = self.bot.database.record_agent_action(
                session_id,
                action.get("kind", ""),
                safe_log_text(user_intent, 1000),
                action.get("summary", ""),
                action.get("payload", {}),
                "proposed",
                result_message="Awaiting user approval",
            )
            proposed.append({"id": action_id, "kind": action.get("kind"), "status": "proposed", "summary": action.get("summary", "")})
        return {"ok": True, "session_id": session_id, "actions": proposed, "skipped": skipped, "count": len(proposed)}

    def apply_action(self, action_id: int = None, session_id: str = "", index=None, confirm: bool = False) -> Dict:
        if action_id:
            return self.apply_recorded_action(int(action_id), confirm=confirm)
        raise ServiceError(400, "Missing action_id")

    def apply_recorded_action(self, action_id: int, confirm: bool = False) -> Dict:
        row = self.bot.database.get_agent_action(action_id)
        if not row:
            raise ServiceError(404, "Agent action #%s not found" % action_id)
        if row["status"] == "applied":
            return {"ok": True, "action_id": action_id, "status": "applied", "message": "Already applied"}
        if row["status"] == "reverted":
            return {"ok": False, "action_id": action_id, "status": "reverted", "message": "Action was reverted"}
        if row["status"] == "pending_confirm" and not confirm:
            return {"ok": False, "action_id": action_id, "status": "pending_confirm", "message": "Typed CONFIRM required"}
        if row["status"] not in ("proposed", "pending_confirm", "failed"):
            raise ServiceError(400, "Agent action #%s cannot be applied from status %s" % (action_id, row["status"]))
        payload = parse_payload(row)
        context = self.action_context(confirmed=confirm)
        try:
            result = apply_agent_action({"kind": row["kind"], "payload": payload}, context)
        except Exception as exc:
            log_context(LOGGER, logging.ERROR, "service_agent_action_exception", action_id=action_id, kind=row["kind"], error=str(exc))
            result = ActionResult(False, "%s: %s" % (exc.__class__.__name__, safe_log_text(exc, 160)))
        status = "pending_confirm" if result.requires_confirm else "applied" if result.applied else "failed"
        self.bot.database.update_agent_action_result(
            action_id,
            status,
            archive_path=result.archive_path or "",
            target_path=result.target_path or "",
            result_message=result.message,
        )
        if result.applied:
            self.after_action_file_change(row["kind"], result.target_path)
        return {
            "ok": bool(result.applied),
            "action_id": action_id,
            "kind": row["kind"],
            "status": status,
            "message": result.message,
            "requires_confirm": result.requires_confirm,
            "target_path": result.target_path,
        }

    def revert_action(self, action_id: int) -> Dict:
        row = self.bot.database.get_agent_action(action_id)
        if not row:
            raise ServiceError(404, "Agent action #%s not found" % action_id)
        if row["status"] == "reverted":
            return {"ok": True, "action_id": action_id, "status": "reverted", "message": "Already reverted"}
        archive_path = Path(row["archive_path"] or "")
        target_path = Path(row["target_path"] or "")
        if not archive_path.exists() or not str(target_path):
            raise ServiceError(400, "Agent action #%s has no reversible archive" % action_id)
        shutil.copyfile(archive_path, target_path)
        self.bot.database.update_agent_action_status(action_id, "reverted")
        revert_id = self.bot.database.record_agent_action(
            row["session_id"],
            "revert",
            "revert %s" % action_id,
            "Reverted action #%s" % action_id,
            {"reverted_action_id": action_id},
            "applied",
            target_path=str(target_path),
            result_message="Restored %s from %s" % (target_path, archive_path),
            revert_target_id=action_id,
        )
        self.after_action_file_change(row["kind"], str(target_path))
        return {"ok": True, "action_id": action_id, "revert_audit_id": revert_id, "status": "reverted"}

    def query_sql(self, sql: str, params: List = None, limit: int = 50) -> Dict:
        if not is_select_only(sql):
            raise ServiceError(400, "Only SELECT SQL is allowed")
        params = params or []
        with self.bot.database.connection() as conn:
            rows = conn.execute(sql, params).fetchmany(min(max(1, limit), 100))
        return {"rows": [row_to_dict(row) for row in rows], "count": len(rows)}

    def process_email(self, body: Dict) -> Dict:
        return self.bot.process_email_alert(
            source_id=str(body.get("source_id") or "email-job-alerts"),
            sender=required(body, "sender"),
            subject=required(body, "subject"),
            body=required(body, "body"),
            message_id=str(body.get("message_id") or ""),
            date=str(body.get("date") or ""),
        )

    def leads_digest(self, limit: int = None, mark_sent: bool = False) -> Dict:
        limit = min(max(1, limit or 10), 25)
        rows = self.bot.database.leads_for_digest(limit)
        leads = [lead_digest_row(row) for row in rows]
        digest_id = ""
        if mark_sent and leads:
            digest_id = self.bot.database.mark_leads_digested([lead["id"] for lead in leads])
        return {"ok": True, "leads": leads, "count": len(leads), "digest_id": digest_id, "marked_sent": bool(digest_id)}

    def research_leads(self, body: Dict) -> Dict:
        candidates = body.get("leads") or body.get("candidates") or []
        if not isinstance(candidates, list):
            raise ServiceError(400, "leads/candidates must be an array")
        if len(candidates) > 25:
            raise ServiceError(400, "At most 25 leads can be saved per approval")
        saved = []
        skipped = []
        for index, candidate in enumerate(candidates):
            try:
                lead = normalize_lead_candidate(candidate)
                lead_id, inserted = self.bot.database.upsert_lead(lead)
                saved.append(
                    {
                        "id": lead_id,
                        "id_prefix": lead_id[:12],
                        "inserted": inserted,
                        "person_name": lead.person_name,
                        "company": lead.company,
                    }
                )
            except ServiceError as exc:
                skipped.append({"index": index, "reason": exc.message})
        session_id = str(body.get("session_id") or "openclaw-leads-%s" % int(time.time() * 1000))
        self.bot.database.record_agent_action(
            session_id,
            "lead_research",
            safe_log_text(body.get("user_intent") or body.get("query") or "", 1000),
            "Saved %s lead candidates" % len(saved),
            {"saved": saved, "skipped": skipped},
            "applied",
            result_message="Lead candidates saved: %s" % len(saved),
        )
        return {"ok": True, "saved": saved, "skipped": skipped, "count": len(saved)}

    def add_lead_source(self, body: Dict) -> Dict:
        source = normalize_lead_source(body)
        source_id, inserted = self.bot.database.upsert_lead_source(source)
        self.bot.database.record_agent_action(
            str(body.get("session_id") or "openclaw-leads-%s" % int(time.time() * 1000)),
            "lead_source",
            safe_log_text(body.get("user_intent") or "", 1000),
            "Added lead source %s" % source.get("name", source_id),
            {"source": source, "source_id": source_id},
            "applied",
            result_message="Lead source saved: %s" % source_id,
        )
        return {"ok": True, "source_id": source_id, "inserted": inserted, "source": source}

    def mark_lead(self, lead_id: str, status: str, details: str = "", snooze_days: int = 7) -> Dict:
        self.ensure_lead(lead_id)
        status = normalize_lead_status(status)
        snoozed_until = None
        if status == "snoozed":
            days = max(1, min(int(snooze_days or 7), 90))
            snoozed_until = (datetime.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat() + "Z"
        self.bot.database.update_lead_status(lead_id, status, details, snoozed_until=snoozed_until)
        self.bot.database.record_agent_action(
            "openclaw-lead-button",
            "mark_lead",
            "inline lead action",
            "Marked lead %s as %s" % (lead_id[:12], status),
            {
                "lead_id": lead_id,
                "status": status,
                "details": details or "",
                "snoozed_until": snoozed_until or "",
            },
            "applied",
            result_message="Lead %s marked as %s" % (lead_id[:12], status),
        )
        return {"ok": True, "lead_id": lead_id, "status": status, "snoozed_until": snoozed_until or ""}

    def draft_lead_pitch(self, lead_id: str, ask: str = "") -> Dict:
        lead = self.ensure_lead(lead_id)
        icp_text = read_text_if_exists(self.bot.config.icp_path)
        draft = ""
        llm_error = ""
        try:
            self.bot.refresh_profile()
            draft = self.bot.llm.lead_pitch(self.bot.profile, icp_text, lead, ask=ask) or ""
        except Exception as exc:  # LLMError, BudgetExceeded, anything else
            llm_error = "%s: %s" % (type(exc).__name__, str(exc))
            draft = ""
        if not draft:
            draft = build_lead_pitch(lead, icp_text, ask)
        self.bot.database.save_lead_draft(lead_id, "dm_pitch", draft)
        return {
            "ok": True,
            "lead_id": lead_id,
            "draft": draft,
            "card_text": format_lead_card(lead_digest_row(lead)),
            "llm_error": llm_error,
        }

    def mark_job(self, job_id: str, status: str, feedback: str, details: str = "") -> Dict:
        self.ensure_job(job_id)
        self.bot.database.update_job_status(job_id, status)
        self.bot.database.add_feedback(job_id, feedback, details=details or None)
        self.audit_mark_job(job_id, status, feedback, details)
        return {"ok": True, "job_id": job_id, "status": status, "feedback": feedback}

    def resolve_job_prefix(self, id_prefix: str) -> Dict:
        prefix = str(id_prefix or "").strip().lower()
        if not JOB_ID_PREFIX_RE.match(prefix):
            raise ServiceError(400, "Job id prefix must be exactly 12 lowercase hex characters")
        with self.bot.database.connection() as conn:
            rows = list(conn.execute("select id from jobs where id like ? order by id asc limit 2", (prefix + "%",)))
        if not rows:
            raise ServiceError(404, "No job matched prefix: %s" % prefix)
        if len(rows) > 1:
            raise ServiceError(409, "Job id prefix is ambiguous: %s" % prefix)
        return {"ok": True, "id_prefix": prefix, "job_id": rows[0]["id"]}

    def resolve_lead_prefix(self, id_prefix: str) -> Dict:
        prefix = str(id_prefix or "").strip().lower()
        if not LEAD_ID_PREFIX_RE.match(prefix):
            raise ServiceError(400, "Lead id prefix must be exactly 12 lowercase hex characters")
        with self.bot.database.connection() as conn:
            rows = list(conn.execute("select id from leads where id like ? order by id asc limit 2", (prefix + "%",)))
        if not rows:
            raise ServiceError(404, "No lead matched prefix: %s" % prefix)
        if len(rows) > 1:
            raise ServiceError(409, "Lead id prefix is ambiguous: %s" % prefix)
        return {"ok": True, "id_prefix": prefix, "lead_id": rows[0]["id"]}

    def audit_mark_job(self, job_id: str, status: str, feedback: str, details: str = "") -> int:
        return self.bot.database.record_agent_action(
            "openclaw-inline-button",
            "mark_job",
            "inline job action",
            "Marked job %s as %s" % (job_id[:12], status),
            {"job_id": job_id, "status": status, "feedback": feedback, "details": details or ""},
            "applied",
            result_message="Job %s marked as %s" % (job_id[:12], status),
        )

    def ensure_job(self, job_id: str):
        job = self.bot.database.get_job(job_id)
        if not job:
            raise ServiceError(404, "Job not found: %s" % safe_log_text(job_id, 120))
        return job

    def ensure_lead(self, lead_id: str):
        lead = self.bot.database.get_lead(lead_id)
        if not lead:
            raise ServiceError(404, "Lead not found: %s" % safe_log_text(lead_id, 120))
        return lead

    def action_context(self, confirmed: bool = False) -> AgentActionContext:
        self.bot.refresh_profile()
        return AgentActionContext(
            config=self.bot.config,
            database=self.bot.database,
            profile=self.bot.profile,
            source_reachable=self.bot.source_candidate_reachable,
            shadow_test=self.bot.scoring.shadow_test,
            run_l2=self.bot.run_l2_relevance,
            confirmed=confirmed,
        )

    def after_action_file_change(self, kind: str, target_path: str = "") -> None:
        path = Path(target_path or "")
        if kind == "sources_proposal" or path == self.bot.config.sources_path:
            self.bot.database.upsert_sources(load_sources(self.bot.config.sources_path))
        if kind == "profile_edit" or path == self.bot.config.profile_path:
            self.bot.refresh_profile()
        if kind == "scoring_rule_proposal" or path == self.bot.config.scoring_path:
            self.bot.rescore_recent_jobs()

    def find_existing_action(self, session_id: str, kind: str, payload: Dict):
        payload_json = json.dumps(payload, sort_keys=True)
        with self.bot.database.connection() as conn:
            return conn.execute(
                """
                select * from agent_actions
                where session_id = ?
                  and kind = ?
                  and payload_json = ?
                  and status in ('proposed', 'applied', 'pending_confirm')
                order by id asc
                limit 1
                """,
                (session_id, kind, payload_json),
            ).fetchone()

    def count_jobs(self) -> int:
        with self.bot.database.connection() as conn:
            return int(conn.execute("select count(*) as c from jobs").fetchone()["c"] or 0)


class ServiceError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def create_handler(app: JobHunterService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JobHunterService/1.0"

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def route(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                body = self.read_json_body() if method == "POST" else {}
                if method == "GET" and path == "/health":
                    payload = app.health()
                elif method == "GET" and path == "/usage":
                    payload = app.usage()
                elif method == "GET" and path == "/profile/show":
                    payload = app.show_profile()
                elif method == "GET" and path == "/history":
                    payload = app.history(int(first(query, "limit", "10")))
                elif method == "POST" and path == "/collect":
                    payload = app.collect()
                elif method == "POST" and path == "/rescore":
                    payload = app.rescore_recent_jobs(optional_int(body.get("limit")) or 500)
                elif method == "POST" and path == "/digest":
                    payload = app.digest(optional_int(body.get("limit")), bool(body.get("mark_sent", False)))
                elif method == "POST" and path == "/irrelevant":
                    payload = app.mark_irrelevant(required(body, "job_id"), str(body.get("details") or ""))
                elif method == "POST" and path == "/applied":
                    payload = app.mark_applied(required(body, "job_id"), str(body.get("details") or ""))
                elif method == "POST" and path == "/snooze":
                    payload = app.snooze(required(body, "job_id"))
                elif method == "POST" and path == "/cover-note":
                    payload = app.cover_note(required(body, "job_id"), bool(body.get("override_budget", False)))
                elif method == "POST" and path == "/jobs/resolve_prefix":
                    payload = app.resolve_job_prefix(required(body, "id_prefix"))
                elif method == "POST" and path == "/leads/resolve_prefix":
                    payload = app.resolve_lead_prefix(required(body, "id_prefix"))
                elif method == "POST" and path == "/action/propose":
                    payload = app.propose_actions(body.get("actions") or [], str(body.get("user_intent") or ""), str(body.get("session_id") or ""))
                elif method == "POST" and path == "/action/apply":
                    payload = app.apply_action(optional_int(body.get("action_id")), str(body.get("session_id") or ""), optional_int(body.get("index")), bool(body.get("confirm", False)))
                elif method == "POST" and path == "/action/revert":
                    payload = app.revert_action(required_int(body, "action_id"))
                elif method == "POST" and path == "/query-sql":
                    payload = app.query_sql(required(body, "sql"), body.get("params") or [], optional_int(body.get("limit")) or 50)
                elif method == "POST" and path == "/email/process":
                    payload = app.process_email(body)
                elif method == "POST" and path == "/leads/digest":
                    payload = app.leads_digest(optional_int(body.get("limit")), bool(body.get("mark_sent", False)))
                elif method == "GET" and path == "/leads/icp/show":
                    payload = app.show_icp()
                elif method == "POST" and path in ("/leads/research", "/leads/save"):
                    payload = app.research_leads(body)
                elif method == "POST" and path == "/leads/source/add":
                    payload = app.add_lead_source(body)
                elif method == "POST" and path == "/leads/mark":
                    payload = app.mark_lead(
                        required(body, "lead_id"),
                        required(body, "status"),
                        str(body.get("details") or ""),
                        optional_int(body.get("snooze_days")) or 7,
                    )
                elif method == "POST" and path == "/leads/pitch":
                    payload = app.draft_lead_pitch(required(body, "lead_id"), str(body.get("ask") or ""))
                else:
                    raise ServiceError(404, "Unknown endpoint: %s %s" % (method, path))
                self.send_json(200, payload)
            except ServiceError as exc:
                self.send_json(exc.status, {"ok": False, "error": exc.message})
            except Exception as exc:
                log_context(LOGGER, logging.ERROR, "service_request_failed", method=method, path=path, error=str(exc))
                self.send_json(500, {"ok": False, "error": "%s: %s" % (exc.__class__.__name__, safe_log_text(exc, 300))})

        def read_json_body(self) -> Dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 1024 * 1024:
                raise ServiceError(413, "Request body too large")
            raw = self.rfile.read(length).decode("utf-8")
            try:
                parsed = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise ServiceError(400, "Invalid JSON: %s" % exc)
            if not isinstance(parsed, dict):
                raise ServiceError(400, "JSON body must be an object")
            return parsed

        def send_json(self, status: int, payload: Dict) -> None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log_context(LOGGER, logging.INFO, "service_http_access", client=self.client_address[0], request_line=fmt % args)

    return Handler


def run(host: str = None, port: int = None) -> None:
    configure_logging()
    host = host or os.getenv("JOBHUNTER_SERVICE_HOST", "127.0.0.1")
    port = port or int(os.getenv("JOBHUNTER_SERVICE_PORT", "8765"))
    app = JobHunterService.from_environment()
    server = ThreadingHTTPServer((host, port), create_handler(app))
    log_context(LOGGER, logging.INFO, "jobhunter_service_started", host=host, port=port)
    server.serve_forever()


def row_to_dict(row) -> Dict:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def job_digest_row(row) -> Dict:
    data = row_to_dict(row)
    for key in ("reasons_json", "concerns_json", "fired_rules_json", "l2_evidence_json"):
        if isinstance(data.get(key), str):
            try:
                data[key.replace("_json", "")] = json.loads(data[key] or "[]")
            except json.JSONDecodeError:
                data[key.replace("_json", "")] = []
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "company": data.get("company"),
        "location": data.get("location"),
        "url": data.get("url"),
        "source_id": data.get("source_id"),
        "source_name": data.get("source_name"),
        "score": data.get("score"),
        "l1_score": data.get("l1_score"),
        "l2_score": data.get("l2_score"),
        "total_score": data.get("total_score"),
        "l2_reason": data.get("l2_reason"),
        "reasons": data.get("reasons", []),
        "concerns": data.get("concerns", []),
        "fired_rules": data.get("fired_rules", []),
    }


def lead_digest_row(row) -> Dict:
    data = row_to_dict(row)
    try:
        evidence = json.loads(data.get("evidence_json") or "[]")
    except json.JSONDecodeError:
        evidence = []
    out = {
        "id": data.get("id"),
        "id_prefix": str(data.get("id") or "")[:12],
        "person_name": data.get("person_name") or "",
        "company": data.get("company") or "",
        "role": data.get("role") or "",
        "url": data.get("url") or "",
        "source_name": data.get("source_name") or "",
        "source_url": data.get("source_url") or "",
        "contact_surface": data.get("contact_surface") or "",
        "evidence": evidence,
        "why_match": data.get("why_match") or "",
        "confidence": int(data.get("confidence") or 0),
        "risk_level": data.get("risk_level") or "low",
        "status": data.get("status") or "new",
        "last_seen_at": data.get("last_seen_at") or "",
    }
    out["card_text"] = format_lead_card(out)
    return out


def format_lead_card(lead: Dict) -> str:
    """Canonical Telegram-friendly lead card text. Stable across send/edit so
    `message(action='edit')` can append the pitch draft without re-formatting."""
    name = lead.get("person_name") or "Unknown"
    role = lead.get("role") or ""
    company = lead.get("company") or ""
    confidence = lead.get("confidence") or 0
    risk = lead.get("risk_level") or "low"
    why = lead.get("why_match") or ""
    evidence = lead.get("evidence") or []
    contact = lead.get("contact_surface") or ""
    url = lead.get("url") or ""
    source_url = lead.get("source_url") or ""
    header_role_company = (
        "%s, %s" % (role, company) if role and company else (role or company)
    )
    header = "**%s**" % name if not header_role_company else "**%s** — %s" % (name, header_role_company)
    lines = [header, "Confidence: %s | Risk: %s" % (confidence, risk)]
    if why:
        lines += ["", "Why match: %s" % why]
    if evidence:
        lines += [""]
        lines.append("Evidence:")
        for item in evidence[:5]:
            text = item if isinstance(item, str) else (item.get("text") or item.get("note") or "")
            if text:
                lines.append("- %s" % text)
    if contact:
        lines += ["", "Contact surface: %s" % contact]
    if url:
        lines.append("Profile/URL: %s" % url)
    if source_url and source_url != url:
        lines.append("Source: %s" % source_url)
    return "\n".join(lines).strip()


def normalize_lead_candidate(candidate) -> Lead:
    if not isinstance(candidate, dict):
        raise ServiceError(400, "Lead candidate must be an object")
    url = first_non_empty(candidate, "url", "profile_url", "evidence_url", "company_url")
    if not url:
        raise ServiceError(400, "Lead candidate needs a public URL")
    validate_lead_url(url)
    source_url = first_non_empty(candidate, "source_url", "source")
    if source_url:
        validate_lead_url(source_url)
    person_name = safe_log_text(first_non_empty(candidate, "person_name", "name"), 160)
    company = safe_log_text(first_non_empty(candidate, "company", "account"), 160)
    role = safe_log_text(first_non_empty(candidate, "role", "title"), 160)
    if not person_name and not company:
        raise ServiceError(400, "Lead candidate needs person_name or company")
    evidence = candidate.get("evidence") or candidate.get("evidence_urls") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    return Lead(
        person_name=person_name,
        company=company,
        role=role,
        url=url.strip(),
        source_name=safe_log_text(first_non_empty(candidate, "source_name"), 160),
        source_url=source_url.strip() if source_url else "",
        contact_surface=safe_log_text(first_non_empty(candidate, "contact_surface", "contact"), 240),
        evidence=[safe_log_text(item, 500) for item in evidence[:8] if str(item).strip()],
        why_match=safe_log_text(first_non_empty(candidate, "why_match", "why", "reason"), 1000),
        confidence=clamp_int(candidate.get("confidence"), 0, 100, 50),
        risk_level=normalize_risk(candidate.get("risk_level") or candidate.get("risk")),
        status=normalize_lead_status(candidate.get("status") or "new"),
        notes=safe_log_text(candidate.get("notes") or "", 1000),
    )


def normalize_lead_source(body: Dict) -> Dict:
    url = required(body, "url")
    validate_lead_url(url)
    source_type = str(body.get("type") or "public_directory").strip().lower()
    allowed = {"public_directory", "company_page", "funding_news", "conference", "community", "api", "other"}
    if source_type not in allowed:
        source_type = "other"
    status = str(body.get("status") or "test").strip().lower()
    if status not in {"test", "active", "disabled"}:
        status = "test"
    return {
        "id": safe_log_text(body.get("id") or "", 80),
        "name": safe_log_text(body.get("name") or url, 160),
        "type": source_type,
        "url": url,
        "status": status,
        "risk_level": normalize_risk(body.get("risk_level") or body.get("risk")),
        "notes": safe_log_text(body.get("notes") or body.get("why") or "", 1000),
        "created_by": "agent",
    }


def normalize_lead_status(value) -> str:
    status = str(value or "new").strip().lower()
    if status == "irrelevant":
        status = "rejected"
    if status not in LEAD_STATUS_VALUES:
        raise ServiceError(400, "Invalid lead status: %s" % safe_log_text(value, 80))
    return status


def validate_lead_url(url: str) -> None:
    try:
        validate_safe_url(url)
    except SourceError as exc:
        raise ServiceError(400, str(exc))


def normalize_risk(value) -> str:
    risk = str(value or "low").strip().lower()
    return risk if risk in {"low", "medium", "high"} else "low"


def first_non_empty(data: Dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def read_text_if_exists(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def build_lead_pitch(lead, icp_text: str, ask: str = "") -> str:
    person = lead["person_name"] or "there"
    company = lead["company"] or "your team"
    role = lead["role"] or "your work"
    why = lead["why_match"] or "the public signals around your company look relevant"
    try:
        evidence = json.loads(lead["evidence_json"] or "[]")
    except json.JSONDecodeError:
        evidence = []
    signal = evidence[0] if evidence else why
    icp_hint = first_sentence(icp_text) or "I work on practical AI automation and product workflows"
    ask_text = ask.strip() or "Worth comparing notes for 15 minutes?"
    return (
        "Hi %s,\n\n"
        "I noticed %s at %s, especially: %s\n\n"
        "%s. It made me think there may be a useful fit around %s.\n\n"
        "%s"
    ) % (person.split()[0], role, company, signal, icp_hint, why, ask_text)


def first_sentence(text: str) -> str:
    cleaned = " ".join((text or "").replace("#", " ").split())
    if not cleaned:
        return ""
    if ". " in cleaned:
        return cleaned.split(". ", 1)[0].strip()[:220]
    return cleaned[:220]


def is_select_only(sql: str) -> bool:
    stripped = (sql or "").strip().lower()
    return stripped.startswith("select") and ";" not in stripped


def required(body: Dict, key: str) -> str:
    value = body.get(key)
    if value is None or str(value).strip() == "":
        raise ServiceError(400, "Missing required field: %s" % key)
    return str(value)


def first(query: Dict[str, List[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def required_int(body: Dict, key: str) -> int:
    value = optional_int(body.get(key))
    if value is None:
        raise ServiceError(400, "Missing required integer field: %s" % key)
    return value


def parse_payload(row) -> Dict:
    try:
        parsed = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise ServiceError(400, "Agent action #%s has invalid payload: %s" % (row["id"], exc))
    if not isinstance(parsed, dict):
        raise ServiceError(400, "Agent action #%s payload must be an object" % row["id"])
    return parsed
