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
from .firecrawl import FirecrawlError, firecrawl_available, firecrawl_search
from .logging_setup import configure_logging, log_context, safe_log_text
from .models import Job, Lead, SourceConfig, utc_now_iso
from .scoring import load_scoring_rules, score_job
from .sources import SourceError, count_known_job_links_in_html, enrich_job_from_url, validate_safe_url

LOGGER = logging.getLogger(__name__)
JOB_ID_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")
LEAD_ID_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")
LEAD_STATUS_VALUES = {"new", "shortlisted", "reached_out", "rejected", "snoozed", "pitched", "archived"}
EMAIL_ALERT_NOISE_TITLES = {
    "apply now",
    "closed to offers",
    "edit job alert",
    "learn more",
    "message from",
    "new jobs",
    "open to offers",
    "read more",
    "ready to interview",
    "unsubscribe",
    "update preferences",
    "view job",
}
EMAIL_ALERT_PLACEHOLDER_COMPANIES = {
    "unknown",
    "unknown company",
}
EMAIL_ALERT_NAVIGATION_URL_TERMS = (
    "email-preferences",
    "job-alert",
    "notification",
    "preferences",
    "settings",
    "unsubscribe",
)
GOALS_TEMPLATE = """# Outcome goals

## Job search
- Target: >=3 applications/week where I'd be net-happy if I got the interview
- Quality bar: <=50% of digest jobs marked Irrelevant
# NOTE: interviews_this_week KPI is computed but currently reads 0 — there is
# no Telegram surface yet to mark a job as interview_request / interview_scheduled.
# Treat the target below as aspirational; do not act on it until the signal exists.
- Target (aspirational): >=1 interview/week from a submitted application

## Lead search
- Target: >=2 leads/week I'd be net-happy reaching out to
- Quality bar: <=40% of digest leads marked Irrelevant
# NOTE: replies_this_week KPI is computed but currently reads 0 — there is no
# surface yet for recording lead replies. Treat the target below as aspirational.
- Target (aspirational): >=30% reply rate on actual outreach sent

## Pipeline health
- Coverage: >=5 distinct sources producing non-Irrelevant rows in any 7-day window
- Latency: median email-arrival to digest <=2h
- No silent failures: a source dark >24h should produce a stakeholder alert

## Cost
- Stay within configured OpenAI daily/monthly budget
# NOTE: firecrawl_calls_today KPI reads 0 today — firecrawl calls happen
# OpenClaw-side and aren't logged in jobhunter-service.usage_log. The hard
# limit below is a documented guardrail, not currently enforced or measured.
- Firecrawl: <=30 scrapes/day across all agents combined (documented guardrail; not yet enforced)

## Constraints (non-negotiable)
- No automated outreach. Drafts only.
- No logged-in scraping. No browser cookies. No LinkedIn auth.
- No impersonation.
- No silent profile/ICP wipes; every edit lands in agent_actions with reason.
- Stakeholder Telegram pings <=1/day under normal operation.
"""

RESEARCH_PLAYBOOK_TEMPLATE = """# Research context

Use this file for deep-research outputs from Claude, ChatGPT, or manual market
research. The Researcher agent operationalizes these notes into source, skill,
or tooling tasks; it should not invent strategy from scratch.

## Current source/lead insights
- Paste recent deep-research findings here.

## Promising job-side angles
- Founding PM at AI-native startups
- AI implementation specialist roles
- Product builder roles using Claude, Codex, or agentic workflows

## Promising lead-side angles
- B2B AI workflow founders
- Domain-expert founders without product leadership
- Recently funded small teams building operational automation

## Validation channels
- Firecrawl/Exa public search
- Public company/job pages
- Funding/news directories
- Product launch directories

## Refresh notes
- Last refreshed: <date/source>
- Next refresh due: <date>
"""


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

    def show_goals(self) -> Dict:
        created = False
        if not self.bot.config.goals_path.exists():
            self.bot.config.goals_path.parent.mkdir(parents=True, exist_ok=True)
            self.bot.config.goals_path.write_text(GOALS_TEMPLATE, encoding="utf-8")
            created = True
        text = read_text_if_exists(self.bot.config.goals_path)
        return {
            "ok": True,
            "path": str(self.bot.config.goals_path),
            "text": text,
            "exists": self.bot.config.goals_path.exists(),
            "created_template": created,
            "parsed": parse_goals_markdown(text),
        }

    def show_research_playbook(self) -> Dict:
        created = False
        if not self.bot.config.research_playbook_path.exists():
            self.bot.config.research_playbook_path.parent.mkdir(parents=True, exist_ok=True)
            self.bot.config.research_playbook_path.write_text(RESEARCH_PLAYBOOK_TEMPLATE, encoding="utf-8")
            created = True
        text = read_text_if_exists(self.bot.config.research_playbook_path)
        return {
            "ok": True,
            "path": str(self.bot.config.research_playbook_path),
            "text": text,
            "exists": self.bot.config.research_playbook_path.exists(),
            "created_template": created,
        }

    def kpi_snapshot(self, window_days: int = 7) -> Dict:
        window_days = min(max(1, int(window_days or 7)), 90)
        cutoff = (datetime.utcnow() - timedelta(days=window_days)).replace(microsecond=0).isoformat() + "Z"
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
        month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
        with self.bot.database.connection() as conn:
            snapshot = {
                "applications_this_week": scalar_int(conn, "select count(*) from jobs where status = 'applied' and first_seen_at >= ?", (cutoff,)),
                "interviews_this_week": scalar_int(
                    conn,
                    "select count(*) from job_feedback where action in ('interview_request','interview_scheduled') and created_at >= ?",
                    (cutoff,),
                ),
                "reach_outs_this_week": scalar_int(conn, "select count(*) from leads where status = 'reached_out' and first_seen_at >= ?", (cutoff,)),
                "replies_this_week": scalar_int(
                    conn,
                    "select count(*) from lead_feedback where action in ('reply','replied','positive_reply') and created_at >= ?",
                    (cutoff,),
                ),
                "active_sources_7d": scalar_int(
                    conn,
                    """
                    select count(distinct source_id)
                    from jobs
                    where first_seen_at >= ?
                      and status not in ('rejected','archived')
                    """,
                    (cutoff,),
                ),
                "openai_spend_today_usd": scalar_float(conn, "select coalesce(sum(estimated_cost_usd), 0) from usage_log where created_at >= ?", (today,)),
                "openai_spend_month_usd": scalar_float(conn, "select coalesce(sum(estimated_cost_usd), 0) from usage_log where created_at >= ?", (month,)),
                "firecrawl_calls_today": scalar_int(conn, "select count(*) from usage_log where task like 'firecrawl%' and created_at >= ?", (today,)),
            }
            job_feedback_total = scalar_int(conn, "select count(*) from job_feedback where created_at >= ?", (cutoff,))
            job_feedback_irrelevant = scalar_int(conn, "select count(*) from job_feedback where action in ('irrelevant','rejected') and created_at >= ?", (cutoff,))
            lead_feedback_total = scalar_int(conn, "select count(*) from lead_feedback where created_at >= ?", (cutoff,))
            lead_feedback_irrelevant = scalar_int(conn, "select count(*) from lead_feedback where action in ('irrelevant','rejected') and created_at >= ?", (cutoff,))
            latency_rows = conn.execute(
                """
                select (julianday(j.first_seen_at) - julianday(e.received_at)) * 24.0 * 60.0 as minutes
                from jobs j
                join email_alert_raw e on e.id = j.email_alert_id
                where e.received_at >= ? and j.first_seen_at is not null
                order by minutes asc
                """,
                (cutoff,),
            ).fetchall()
        snapshot["irrelevant_rate_jobs_7d"] = ratio(job_feedback_irrelevant, job_feedback_total)
        snapshot["irrelevant_rate_leads_7d"] = ratio(lead_feedback_irrelevant, lead_feedback_total)
        snapshot["latency_email_to_digest_p50_minutes"] = median([float(row["minutes"] or 0) for row in latency_rows])
        return {"ok": True, "window_days": window_days, "kpis": snapshot}

    def kpi_history(self, weeks: int = 8) -> Dict:
        weeks = min(max(1, int(weeks or 8)), 26)
        history = []
        for offset in range(weeks):
            end = datetime.utcnow() - timedelta(days=offset * 7)
            start = end - timedelta(days=7)
            history.append(self.kpi_window(start, end))
        return {"ok": True, "weeks": weeks, "history": history}

    def kpi_window(self, start: datetime, end: datetime) -> Dict:
        start_iso = start.replace(microsecond=0).isoformat() + "Z"
        end_iso = end.replace(microsecond=0).isoformat() + "Z"
        with self.bot.database.connection() as conn:
            return {
                "week_start": start_iso[:10],
                "week_end": end_iso[:10],
                "applications": scalar_int(conn, "select count(*) from jobs where status = 'applied' and first_seen_at >= ? and first_seen_at < ?", (start_iso, end_iso)),
                "reach_outs": scalar_int(conn, "select count(*) from leads where status = 'reached_out' and first_seen_at >= ? and first_seen_at < ?", (start_iso, end_iso)),
                "irrelevant_jobs": scalar_int(conn, "select count(*) from job_feedback where action in ('irrelevant','rejected') and created_at >= ? and created_at < ?", (start_iso, end_iso)),
                "active_sources": scalar_int(conn, "select count(distinct source_id) from jobs where first_seen_at >= ? and first_seen_at < ? and status not in ('rejected','archived')", (start_iso, end_iso)),
            }

    def apply_directive_edit(self, body: Dict) -> Dict:
        reason = required(body, "reason")
        context = self.action_context()
        result = apply_agent_action({"kind": "directive_edit", "payload": {"directive": required(body, "text")}}, context)
        if not result.applied:
            raise ServiceError(400, result.message)
        action_id = self.bot.database.record_agent_action(
            "pm-direct",
            "directive_edit",
            "PM direct hypothesis edit",
            "PM appended a profile directive",
            {"directive": required(body, "text"), "reason": reason},
            "applied_by_pm",
            archive_path=result.archive_path,
            target_path=result.target_path,
            result_message=reason,
        )
        self.after_action_file_change("directive_edit", result.target_path)
        return {"ok": True, "action_id": action_id, "archive_path": result.archive_path, "message": result.message}

    def apply_icp_edit(self, body: Dict) -> Dict:
        reason = required(body, "reason")
        context = self.action_context()
        result = apply_agent_action({"kind": "icp_edit", "payload": {"new_icp": required(body, "text")}}, context)
        if not result.applied:
            raise ServiceError(400, result.message)
        action_id = self.bot.database.record_agent_action(
            "pm-direct",
            "icp_edit",
            "PM direct ICP edit",
            "PM replaced Leadhunter ICP",
            {"new_icp": required(body, "text"), "reason": reason},
            "applied_by_pm",
            archive_path=result.archive_path,
            target_path=result.target_path,
            result_message=reason,
        )
        return {"ok": True, "action_id": action_id, "archive_path": result.archive_path, "message": result.message}

    def set_source_status(self, body: Dict) -> Dict:
        status = required(body, "status")
        if status not in {"active", "test", "disabled"}:
            raise ServiceError(400, "status must be active, test, or disabled")
        return self.update_source_field(required(body, "source_id"), "status", status, required(body, "reason"))

    def set_source_priority(self, body: Dict) -> Dict:
        priority = required(body, "priority")
        if priority not in {"high", "medium", "low"}:
            raise ServiceError(400, "priority must be high, medium, or low")
        return self.update_source_field(required(body, "source_id"), "priority", priority, required(body, "reason"))

    def update_source_field(self, source_id: str, field: str, value: str, reason: str) -> Dict:
        rows = load_json_array(self.bot.config.sources_path)
        matched = False
        for row in rows:
            if str(row.get("id")) != source_id:
                continue
            row[field] = value
            if field == "status":
                row["enabled"] = value != "disabled"
            matched = True
            break
        if not matched:
            raise ServiceError(404, "Source not found: %s" % safe_log_text(source_id, 120))
        archive = archive_file(self.bot.config.sources_path)
        self.bot.config.sources_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.bot.database.upsert_sources(load_sources(self.bot.config.sources_path))
        kind = "source_%s_set" % field
        action_id = self.bot.database.record_agent_action(
            "pm-direct",
            kind,
            "PM direct source edit",
            "PM set source %s for %s" % (field, source_id),
            {"source_id": source_id, field: value, "reason": reason},
            "applied_by_pm",
            archive_path=str(archive),
            target_path=str(self.bot.config.sources_path),
            result_message=reason,
        )
        return {"ok": True, "action_id": action_id, "source_id": source_id, field: value, "archive_path": str(archive)}

    def history(self, limit: int = 10) -> Dict:
        return {"actions": [row_to_dict(row) for row in self.bot.database.recent_agent_actions(limit)]}

    def file_task(self, body: Dict) -> Dict:
        try:
            task_id = self.bot.database.file_agent_task(
                str(body.get("from_agent") or "user"),
                required(body, "to_agent"),
                required(body, "kind"),
                required(body, "summary"),
                body.get("payload") if isinstance(body.get("payload"), dict) else {},
                optional_int(body.get("priority")) if optional_int(body.get("priority")) is not None else 50,
            )
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        return {"ok": True, "task_id": task_id}

    def pick_task(self, body: Dict) -> Dict:
        kinds = body.get("kinds") or []
        if isinstance(kinds, str):
            kinds = [kinds]
        if not isinstance(kinds, list):
            raise ServiceError(400, "kinds must be an array")
        try:
            row = self.bot.database.pick_agent_task(
                required(body, "agent"),
                kinds=[str(kind) for kind in kinds if str(kind).strip()],
                max_age_days=optional_int(body.get("max_age_days")),
            )
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        return {"ok": True, "task": row_to_dict(row) if row else None}

    def complete_task(self, body: Dict) -> Dict:
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        try:
            row = self.bot.database.complete_agent_task(required_int(body, "task_id"), required(body, "status"), result)
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        if not row:
            raise ServiceError(404, "Agent task not found")
        return {"ok": True, "task": row_to_dict(row)}

    def write_status_report(self, body: Dict) -> Dict:
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        try:
            report_id = self.bot.database.write_agent_report(
                required(body, "agent"),
                required(body, "summary"),
                details,
                str(body.get("report_date") or ""),
            )
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        return {"ok": True, "report_id": report_id}

    def read_reports(self, limit: int = 20, agent: str = "", since: str = "") -> Dict:
        try:
            rows = self.bot.database.read_agent_reports(agent=agent, since=since, limit=limit)
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        return {"ok": True, "reports": [row_to_dict(row) for row in rows], "count": len(rows)}

    def list_open_tasks(self, body: Dict) -> Dict:
        try:
            rows = self.bot.database.list_agent_tasks(
                to_agent=str(body.get("to_agent") or ""),
                from_agent=str(body.get("from_agent") or ""),
                status=str(body.get("status") or "open"),
                limit=optional_int(body.get("limit")) or 20,
            )
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        return {"ok": True, "tasks": [row_to_dict(row) for row in rows], "count": len(rows)}

    def collect(self) -> Dict:
        before = self.count_jobs()
        self.bot.collect()
        after = self.count_jobs()
        return {
            "ok": True,
            "jobs_before": before,
            "jobs_after": after,
            "inserted_estimate": max(0, after - before),
            "unparsed_email_count": self.bot.database.unparsed_email_count(),
        }

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
        payload = {
            "jobs": jobs,
            "count": len(jobs),
            "digest_id": digest_id,
            "marked_sent": bool(digest_id),
            "unparsed_email_count": self.bot.database.unparsed_email_count(),
        }
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
        return {
            "ok": True,
            "job_id": job_id,
            "draft": draft,
            "card_text": format_job_card(job_digest_row(job)),
        }

    def find_job_recruiters(self, job_id: str) -> Dict:
        job = self.ensure_job(job_id)
        result = self._find_linkedin_profiles(
            job["company"],
            "recruiter",
            company_url=job["url"] if "url" in job.keys() else "",
        )
        result["job_id"] = job_id
        result["card_text"] = format_job_card(job_digest_row(job))
        return result

    def find_lead_linkedin(self, lead_id: str) -> Dict:
        lead = self.bot.database.get_lead(lead_id)
        if not lead:
            raise ServiceError(404, "Lead %s not found" % lead_id)
        company = lead["company"] if "company" in lead.keys() else ""
        person_name = lead["person_name"] if "person_name" in lead.keys() else ""
        # Note: lead.url is typically the discovery URL (Antler announcement, TechCrunch
        # article, Product Hunt page, YC Launches post) — NOT the lead company's own
        # website. Passing it as company_url would extract an unrelated domain and skew
        # Google toward results mentioning the source, not the target company. Skip it.
        result = self._find_linkedin_profiles(
            company,
            "founder",
            company_url="",
            prefer_name_substring=person_name,
        )
        result["lead_id"] = lead_id
        return result

    def _find_linkedin_profiles(
        self,
        company: str,
        kind: str,
        company_url: str = "",
        prefer_name_substring: str = "",
    ) -> Dict:
        if kind not in ("recruiter", "founder"):
            raise ServiceError(400, "kind must be 'recruiter' or 'founder'")
        company_label = (company or "").strip()
        if not company_label:
            return {"ok": True, "company": "", "kind": kind, "profiles": [], "source": "no_company"}
        company_normalized = _normalize_company_for_cache(company_label)
        cached = self.bot.database.get_linkedin_cache(company_normalized, kind)
        if cached:
            fetched_at = cached["fetched_at"]
            try:
                age_days = (datetime.utcnow() - datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ")).days
            except Exception:
                age_days = 999
            if age_days < 30:
                profiles = json.loads(cached["profiles_json"] or "[]")
                if prefer_name_substring:
                    profiles = _reorder_by_name_match(profiles, prefer_name_substring)
                return {
                    "ok": True,
                    "company": company_label,
                    "kind": kind,
                    "profiles": profiles,
                    "source": "cache",
                    "cache_age_days": age_days,
                }
        if not firecrawl_available(self.bot.config.firecrawl_api_key):
            raise ServiceError(503, "Firecrawl is not configured (FIRECRAWL_API_KEY missing)")
        if kind == "recruiter":
            role_clause = '(recruiter OR "talent acquisition" OR "people operations" OR "head of people" OR sourcer)'
        else:
            role_clause = '(founder OR CEO OR "co-founder" OR "chief executive" OR "founding")'
        domain_hint = _company_domain_hint(company_url)
        if domain_hint:
            query = 'site:linkedin.com/in ("%s" OR "%s") %s' % (company_label, domain_hint, role_clause)
        else:
            query = 'site:linkedin.com/in "%s" %s' % (company_label, role_clause)
        try:
            search = firecrawl_search(query, api_key=self.bot.config.firecrawl_api_key, limit=10)
        except FirecrawlError as exc:
            raise ServiceError(502, "Firecrawl search failed: %s" % exc)
        profiles = _parse_linkedin_search_results(search.get("results") or [], domain_hint=domain_hint)
        if prefer_name_substring:
            profiles = _reorder_by_name_match(profiles, prefer_name_substring)
        # Cache the un-reordered top-5 so callers without person_name see consistent rankings.
        self.bot.database.upsert_linkedin_cache(
            company_normalized,
            kind,
            json.dumps(profiles, ensure_ascii=False),
            source_company_label=company_label,
        )
        return {
            "ok": True,
            "company": company_label,
            "kind": kind,
            "profiles": profiles,
            "source": "firecrawl",
        }

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

    def list_email_alerts(self, limit: int = 20, since: str = "", only_unparsed: bool = False) -> Dict:
        rows = self.bot.database.list_email_alerts(limit=limit, since=since, only_unparsed=only_unparsed)
        return {"ok": True, "alerts": [row_to_dict(row) for row in rows], "count": len(rows)}

    def email_alert_compare(self, email_alert_id: int) -> Dict:
        payload = self.bot.database.email_alert_compare(email_alert_id)
        if not payload:
            raise ServiceError(404, "Email alert not found: %s" % email_alert_id)
        return {"ok": True, "email_alert": payload}

    def unparsed_emails(self, limit: int = 20) -> Dict:
        emails = self.bot.database.unparsed_email_alerts(limit)
        return {"ok": True, "emails": emails, "count": len(emails)}

    def save_extracted_email_jobs(self, body: Dict) -> Dict:
        email_alert_id = required_int(body, "email_alert_id")
        alert = self.bot.database.email_alert_compare(email_alert_id)
        if not alert:
            raise ServiceError(404, "Email alert not found: %s" % email_alert_id)
        raw_jobs = body.get("jobs")
        if not isinstance(raw_jobs, list):
            raise ServiceError(400, "jobs must be an array")
        if len(raw_jobs) > 50:
            raise ServiceError(400, "At most 50 jobs can be saved per email")
        ruleset = load_scoring_rules(self.bot.config.scoring_path)
        source_id = alert.get("source_id") or "email-job-alerts"
        source_name = self.source_name_for(source_id)
        saved = []
        skipped = []
        enriched = 0
        enrich_failed = 0
        l2_candidates = []
        source = SourceConfig(id=source_id, name=source_name, type="imap", url="imap://job-alerts")
        for index, raw_job in enumerate(raw_jobs):
            try:
                job = normalize_extracted_alert_job(raw_job, source_id, source_name)
            except ServiceError as exc:
                skipped.append({"index": index, "reason": exc.message})
                continue
            job.email_alert_id = email_alert_id
            job_id, inserted = self.bot.database.upsert_job(job)
            enrichment = self.enrich_job_description(job_id, fail_soft=True)
            if enrichment.get("status") == "enriched":
                enriched += 1
            else:
                enrich_failed += 1
            row = self.bot.database.get_job(job_id)
            if row:
                scored_job = job_from_row(row)
                result = score_job(scored_job, self.bot.profile, ruleset)
                self.bot.database.save_score(job_id, result)
                if self.bot.should_l2_score(source, scored_job, result.score, len(l2_candidates)):
                    refreshed = self.bot.database.get_job(job_id)
                    if refreshed:
                        l2_candidates.append(refreshed)
            saved.append({"id": job_id, "id_prefix": job_id[:12], "inserted": inserted, "url": job.url})
        self.bot.database.mark_email_alert_parsed(email_alert_id, len(saved))
        if l2_candidates:
            self.bot.run_l2_relevance(l2_candidates)
        return {
            "ok": True,
            "email_alert_id": email_alert_id,
            "saved": len(saved),
            "enriched": enriched,
            "enrich_failed": enrich_failed,
            "skipped": skipped,
            "jobs": saved,
            "l2_candidates": len(l2_candidates),
        }

    def enrich_job_description(self, job_id: str, fail_soft: bool = False) -> Dict:
        row = self.bot.database.get_job(job_id)
        if not row:
            raise ServiceError(404, "Job not found: %s" % safe_log_text(job_id, 120))
        fields = enrich_job_from_url(row)
        status = fields.pop("enrich_status", "skipped")
        error = fields.pop("error", "")
        try:
            self.bot.database.update_job_enrichment(job_id, fields)
        except Exception as exc:
            if not fail_soft:
                raise
            status = "failed"
            error = "%s: %s" % (exc.__class__.__name__, safe_log_text(exc, 200))
        updated = self.bot.database.get_job(job_id)
        description_length = len((updated["description"] if updated else row["description"]) or "")
        return {
            "ok": status != "failed" or fail_soft,
            "job_id": job_id,
            "status": status,
            "description_length": description_length,
            "error": error,
        }

    def audit_email_extraction(
        self,
        days: int = 7,
        threshold: float = 0.5,
        min_expected: int = 3,
        unmark: bool = False,
    ) -> Dict:
        """Detect emails that look under-extracted (parsed_jobs_count low vs known
        job-link count in raw_html) within the given window. If unmark=True,
        clears parsed_at on flagged rows so they re-enter the queue."""
        try:
            threshold = max(0.0, min(1.0, float(threshold)))
        except (TypeError, ValueError):
            threshold = 0.5
        min_expected = max(1, int(min_expected or 1))
        alerts = self.bot.database.email_alerts_for_audit(days)
        suspicious = []
        checked = 0
        for alert in alerts:
            if alert.get("parsed_at") is None:
                continue
            checked += 1
            expected = count_known_job_links_in_html(alert.get("raw_html") or "")
            if expected < min_expected:
                continue
            parsed = int(alert.get("parsed_jobs_count") or 0)
            if parsed < expected * threshold:
                suspicious.append(
                    {
                        "email_alert_id": alert["id"],
                        "sender": alert.get("sender") or "",
                        "subject": alert.get("subject") or "",
                        "received_at": alert.get("received_at"),
                        "parsed_jobs_count": parsed,
                        "expected_min": expected,
                    }
                )
        unmarked = 0
        if unmark and suspicious:
            unmarked = self.bot.database.unmark_email_parsed(
                [s["email_alert_id"] for s in suspicious]
            )
        return {
            "ok": True,
            "days": int(days),
            "threshold": threshold,
            "min_expected": min_expected,
            "checked": checked,
            "suspicious": suspicious,
            "suspicious_count": len(suspicious),
            "unmarked": unmarked,
        }

    def unmark_email_parsed(self, email_alert_ids: List[int]) -> Dict:
        ids = [int(i) for i in (email_alert_ids or []) if i is not None]
        unmarked = self.bot.database.unmark_email_parsed(ids)
        return {"ok": True, "requested": len(ids), "unmarked": unmarked}

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
        return {
            "ok": True,
            "leads": leads,
            "count": len(leads),
            "digest_id": digest_id,
            "marked_sent": bool(digest_id),
            **leads_freshness(self.bot.database),
        }

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

    def rescore_leads(self, body: Dict) -> Dict:
        """Re-evaluate confidence + why_match for saved leads against the CURRENT ICP.

        Body params (all optional):
        - statuses: list of lead statuses to include, default ['new', 'shortlisted']
        - limit: cap on rows processed in one call, default 50, max 200
        - override_budget: bool, default False — set true to force run when budget gate would block

        Returns summary with per-lead deltas and biggest movers; persists via update_lead_score.
        Each run records ONE agent_actions audit row with kind='lead_rescore' and status='applied'.
        """
        statuses = body.get("statuses") or ["new", "shortlisted"]
        if not isinstance(statuses, list) or not statuses:
            raise ServiceError(400, "statuses must be a non-empty list")
        bad = [s for s in statuses if s not in LEAD_STATUS_VALUES]
        if bad:
            raise ServiceError(400, "unknown lead statuses: %s" % ", ".join(bad))
        # Default batch is 8 (about 24 seconds at gpt-4o-mini latency, under
        # Codex's 30-second per-tool-call timeout). Caller can pass a larger
        # limit if they have headroom — service caps at 200.
        limit = max(1, min(int(body.get("limit") or 8), 200))
        override_budget = bool(body.get("override_budget", False))

        icp_text = read_text_if_exists(self.bot.config.icp_path)
        if not icp_text.strip():
            raise ServiceError(400, "Leadhunter ICP is empty — set it via icp_edit before rescoring")
        self.bot.refresh_profile()
        # Wave tracking: caller passes `wave_start` from the previous call's
        # response to continue the same wave; absent/empty means start a new
        # wave NOW. Use microsecond precision so it sorts correctly against
        # update_lead_score's microsecond-precise last_seen_at (ISO-8601 string
        # comparison treats "...:00.123456Z" as LESS than "...:00Z" because
        # "." < "Z" in ASCII — both ends of the comparison must use the same
        # fractional format).
        wave_start = str(body.get("wave_start") or "").strip()
        if not wave_start:
            wave_start = datetime.utcnow().isoformat(timespec="microseconds") + "Z"
        rows = self.bot.database.leads_for_rescore(statuses, limit)
        if not rows:
            return {"ok": True, "rescored": 0, "skipped": 0, "errors": 0, "deltas": [], "biggest_movers": [], "message": "No leads matched the requested statuses"}

        deltas = []  # one entry per successfully rescored lead
        skipped = 0
        errors: List[str] = []
        for row in rows:
            previous = int(row["confidence"] or 0)
            previous_why = (row["why_match"] or "")
            try:
                result = self.bot.llm.lead_score(self.bot.profile, icp_text, row, override_budget=override_budget)
            except Exception as exc:  # LLMError, BudgetExceeded, etc.
                errors.append("%s: %s" % (row["id"][:12], safe_log_text(exc, 160)))
                skipped += 1
                continue
            if not result:
                skipped += 1
                continue
            new_conf = int(result["confidence"])
            new_why = result["why_match"]
            if new_conf == previous and new_why.strip() == previous_why.strip():
                # No-op update — still bump last_seen_at so user can see the rescore happened
                self.bot.database.update_lead_score(row["id"], new_conf, new_why)
                deltas.append({"lead_id": row["id"], "company": row["company"] or "", "person_name": row["person_name"] or "", "previous": previous, "new": new_conf, "delta": 0})
                continue
            self.bot.database.update_lead_score(row["id"], new_conf, new_why)
            deltas.append({
                "lead_id": row["id"],
                "company": row["company"] or "",
                "person_name": row["person_name"] or "",
                "previous": previous,
                "new": new_conf,
                "delta": new_conf - previous,
            })

        biggest_movers = sorted(deltas, key=lambda d: abs(d["delta"]), reverse=True)[:5]
        # Leads still older than the wave start are not yet rescored. Codex
        # loops by re-calling with the same wave_start until remaining=0.
        remaining = self.bot.database.count_leads_not_yet_rescored(statuses, wave_start)
        wave_done = remaining == 0
        summary = {
            "rescored": len(deltas),
            "skipped": skipped,
            "errors_count": len(errors),
            "errors": errors[:5],
            "biggest_movers": biggest_movers,
            "statuses": statuses,
            "limit": limit,
            "wave_start": wave_start,
            "remaining": remaining,
            "wave_done": wave_done,
        }
        # Single audit row per BATCH (the loop produces N audit rows, one per
        # tool call). Each row is searchable via jobhunter_history with kind=lead_rescore.
        self.bot.database.record_agent_action(
            "rescore-leads",
            "lead_rescore",
            "User-requested lead rescore against current ICP",
            "Rescored %s lead(s), %s skipped, %s errored; remaining=%s" % (len(deltas), skipped, len(errors), remaining),
            summary,
            "applied",
            result_message="Rescored %s of %s leads (wave remaining %s)" % (len(deltas), len(rows), remaining),
        )
        return {"ok": True, **summary, "checked": len(rows), "deltas": deltas}

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

    def source_name_for(self, source_id: str) -> str:
        with self.bot.database.connection() as conn:
            row = conn.execute("select name from sources where id = ?", (source_id,)).fetchone()
        return row["name"] if row and row["name"] else "Email Alerts"


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
                elif method == "GET" and path == "/goals/show":
                    payload = app.show_goals()
                elif method == "GET" and path == "/research/playbook/show":
                    payload = app.show_research_playbook()
                elif method == "GET" and path == "/kpi/snapshot":
                    payload = app.kpi_snapshot(optional_int(first(query, "window_days", "")) or 7)
                elif method == "GET" and path == "/kpi/history":
                    payload = app.kpi_history(optional_int(first(query, "weeks", "")) or 8)
                elif method == "GET" and path == "/history":
                    payload = app.history(int(first(query, "limit", "10")))
                elif method == "GET" and path == "/agent/reports":
                    payload = app.read_reports(
                        optional_int(first(query, "limit", "")) or 20,
                        str(first(query, "agent", "") or ""),
                        str(first(query, "since", "") or ""),
                    )
                elif method == "GET" and path == "/email/alerts":
                    payload = app.list_email_alerts(
                        optional_int(first(query, "limit", "")) or 20,
                        str(first(query, "since", "") or ""),
                        boolish(first(query, "only_unparsed", "")),
                    )
                elif method == "GET" and path == "/email/alert/compare":
                    email_alert_id = optional_int(first(query, "id", ""))
                    if email_alert_id is None:
                        raise ServiceError(400, "Missing required integer field: id")
                    payload = app.email_alert_compare(email_alert_id)
                elif method == "GET" and path == "/email/unparsed":
                    payload = app.unparsed_emails(optional_int(first(query, "limit", "")) or 20)
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
                elif method == "POST" and path == "/jobs/find_recruiters":
                    payload = app.find_job_recruiters(required(body, "job_id"))
                elif method == "POST" and path == "/leads/find_linkedin":
                    payload = app.find_lead_linkedin(required(body, "lead_id"))
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
                elif method == "POST" and path == "/pm/directive":
                    payload = app.apply_directive_edit(body)
                elif method == "POST" and path == "/pm/icp":
                    payload = app.apply_icp_edit(body)
                elif method == "POST" and path == "/pm/source/status":
                    payload = app.set_source_status(body)
                elif method == "POST" and path == "/pm/source/priority":
                    payload = app.set_source_priority(body)
                elif method == "POST" and path == "/query-sql":
                    payload = app.query_sql(required(body, "sql"), body.get("params") or [], optional_int(body.get("limit")) or 50)
                elif method == "POST" and path == "/agent/task/file":
                    payload = app.file_task(body)
                elif method == "POST" and path == "/agent/task/pick":
                    payload = app.pick_task(body)
                elif method == "POST" and path == "/agent/task/complete":
                    payload = app.complete_task(body)
                elif method == "POST" and path == "/agent/task/list":
                    payload = app.list_open_tasks(body)
                elif method == "POST" and path == "/agent/report/write":
                    payload = app.write_status_report(body)
                elif method == "POST" and path == "/email/process":
                    payload = app.process_email(body)
                elif method == "POST" and path == "/email/save_extracted_jobs":
                    payload = app.save_extracted_email_jobs(body)
                elif method == "POST" and path == "/email/enrich_job_description":
                    payload = app.enrich_job_description(required(body, "job_id"))
                elif method == "POST" and path == "/email/audit_extraction":
                    payload = app.audit_email_extraction(
                        days=optional_int(body.get("days")) or 7,
                        threshold=float(body.get("threshold") or 0.5),
                        min_expected=optional_int(body.get("min_expected")) or 3,
                        unmark=bool(body.get("unmark", False)),
                    )
                elif method == "POST" and path == "/email/unmark_parsed":
                    ids = body.get("email_alert_ids")
                    if not isinstance(ids, list):
                        single = optional_int(body.get("email_alert_id"))
                        ids = [single] if single is not None else []
                    payload = app.unmark_email_parsed(ids)
                elif method == "POST" and path == "/leads/rescore":
                    payload = app.rescore_leads(body)
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


def parse_goals_markdown(text: str) -> Dict:
    current = ""
    kpis = []
    constraints = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip()
            continue
        if not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        record = {"section": current, "text": item}
        if current.lower().startswith("constraints"):
            constraints.append(record)
        else:
            kpis.append(record)
    return {"kpis": kpis, "constraints": constraints}


def scalar_int(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def scalar_float(conn, sql: str, params=()) -> float:
    row = conn.execute(sql, params).fetchone()
    return float(row[0] or 0)


def ratio(numerator: int, denominator: int):
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)


def median(values: List[float]):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)


def load_json_array(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as exc:
        raise ServiceError(400, "Invalid JSON in %s: %s" % (path, exc))
    if not isinstance(parsed, list):
        raise ServiceError(400, "%s must contain a JSON array" % path)
    return [row for row in parsed if isinstance(row, dict)]


def archive_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    archive = path.with_name("%s.%s.bak" % (path.name, datetime.utcnow().strftime("%Y%m%d%H%M%S%f")))
    shutil.copyfile(path, archive)
    return archive


def job_digest_row(row) -> Dict:
    data = row_to_dict(row)
    for key in ("reasons_json", "concerns_json", "fired_rules_json", "l2_evidence_json"):
        if isinstance(data.get(key), str):
            try:
                data[key.replace("_json", "")] = json.loads(data[key] or "[]")
            except json.JSONDecodeError:
                data[key.replace("_json", "")] = []
    out = {
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
    out["card_text"] = format_job_card(out)
    return out


def _normalize_company_for_cache(company: str) -> str:
    """Stable cache key — lowercase, strip suffixes like Inc/LLC/Ltd, collapse whitespace."""
    text = (company or "").strip().lower()
    text = re.sub(r"[‘’“”\"',]", "", text)
    text = re.sub(
        r"[\s,]+(inc|llc|ltd|gmbh|sa|s\.a\.|s\.r\.l|s\.r\.o|pty|co|company|corp|corporation|holdings|labs|ai|io)\.?$",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _company_domain_hint(url: str) -> str:
    """Return the company-owned domain (e.g. 'substrate.run') if `url` is not a known
    ATS or job-aggregator host, else ''. Used to add an OR-clause to the LinkedIn search
    to disambiguate companies with collision-prone names.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    netloc = (parsed.netloc or "").lower().strip()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if not netloc or "." not in netloc:
        return ""
    for suffix in _ATS_AGGREGATOR_HOSTS:
        if netloc == suffix or netloc.endswith("." + suffix):
            return ""
    return netloc


_ATS_AGGREGATOR_HOSTS = {
    # ATS providers
    "ashbyhq.com", "greenhouse.io", "lever.co", "workable.com", "recruitee.com",
    "personio.de", "personio.com", "smartrecruiters.com", "bamboohr.com",
    # Job aggregators / boards
    "linkedin.com", "wellfound.com", "angel.co", "angellist.com", "indeed.com",
    "glassdoor.com", "monster.com", "ziprecruiter.com",
    "ycombinator.com", "lobste.rs", "news.ycombinator.com", "hnhiring.com",
    "weworkremotely.com", "remoteok.com", "remoteok.io", "remotive.com",
    "himalayas.app", "arbeitnow.com", "djinni.co", "dou.ua",
    "vibehackers.io", "goodvibecode.com", "realworkfromanywhere.com",
    "producthunt.com", "betalist.com", "crunchbase.com", "github.com",
    # Founder/lead discovery sources (their domain is NOT the lead company's domain)
    "antler.co", "techcrunch.com", "techstars.com", "pear.vc",
    "tinyseed.com", "forumvc.com", "eu-startups.com", "sifted.eu",
    "joinef.com", "f.inc", "aigrant.com", "southparkcommons.com",
    "speedrun.a16z.com", "a16z.com", "sequoiacap.com",
    "brokertechventures.com", "fintechinnovationlab.com", "gener8tor.com",
    "nar-reach.com",
}


def _reorder_by_name_match(profiles: List[Dict], substring: str) -> List[Dict]:
    """Move profiles whose name or URL slug contains `substring` (case-insensitive) to the front.
    Stable sort: preserves original order within each group.
    """
    substr = (substring or "").strip().lower()
    if not substr or not profiles:
        return profiles
    return sorted(
        profiles,
        key=lambda p: 0 if (substr in (p.get("name") or "").lower() or substr in (p.get("url") or "").lower()) else 1,
    )


def _parse_linkedin_search_results(results: List, domain_hint: str = "") -> List[Dict]:
    """Filter firecrawl search results to LinkedIn /in/ profile URLs and extract name + title hint.

    Returns up to 5 profiles with shape `{url, name, title_hint, snippet}`. Drops non-LinkedIn
    URLs, /company/ pages, and obvious-noise titles. When `domain_hint` is provided, prefers
    results whose snippet mentions that domain (helps disambiguate name collisions).
    """
    candidates = []
    seen = set()
    domain_lc = (domain_hint or "").strip().lower()
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        m = re.match(r"^https?://([a-z\-]+\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)/?", url)
        if not m:
            continue
        slug = m.group(2).lower()
        if slug in seen:
            continue
        seen.add(slug)
        title = str(entry.get("title") or "").strip()
        snippet = str(entry.get("description") or entry.get("snippet") or "").strip()
        # Title pattern is usually "Name - Title - Company | LinkedIn"
        name = ""
        title_hint = ""
        if title:
            cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
            parts = [p.strip() for p in re.split(r"\s+[–—\-]\s+", cleaned) if p.strip()]
            if parts:
                name = parts[0]
                if len(parts) > 1:
                    title_hint = " — ".join(parts[1:])
        profile = {
            "url": "https://www.linkedin.com/in/%s" % slug,
            "name": name[:120],
            "title_hint": title_hint[:160],
            "snippet": snippet[:280],
        }
        # Domain-hint preference: 0 = snippet mentions domain (high confidence), 1 = otherwise
        if domain_lc and (domain_lc in snippet.lower() or domain_lc in title.lower()):
            profile["_rank"] = 0
        else:
            profile["_rank"] = 1
        candidates.append(profile)
    candidates.sort(key=lambda p: p["_rank"])
    out = []
    for p in candidates[:5]:
        p.pop("_rank", None)
        out.append(p)
    return out


def format_job_card(job: Dict) -> str:
    """Canonical Telegram-friendly job card text. Stable across send/edit so the
    cover callback can append cleanly to the original message body."""
    title = job.get("title") or "Untitled role"
    company = job.get("company") or "Unknown company"
    total = job.get("total_score")
    location = job.get("location") or ""
    l2_reason = job.get("l2_reason") or ""
    reasons = job.get("reasons") or []
    url = job.get("url") or ""
    header = "**%s** — %s" % (title, company)
    if total is not None:
        header += " — score %s" % total
    lines = [header]
    if location:
        lines.append(location)
    if l2_reason:
        lines += ["", l2_reason]
    elif reasons:
        first = reasons[0]
        text = first if isinstance(first, str) else (first.get("text") or first.get("note") or "")
        if text:
            lines += ["", text]
    if url:
        lines += ["", url]
    return "\n".join(lines).strip()


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


LEADS_STALE_HOURS = 24


def leads_freshness(database) -> Dict:
    last = database.leads_last_seen_at()
    if not last:
        return {
            "leads_last_seen_at": None,
            "leads_freshness_minutes": None,
            "leads_freshness_hours": None,
            "leads_is_stale": True,
        }
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return {
            "leads_last_seen_at": str(last),
            "leads_freshness_minutes": None,
            "leads_freshness_hours": None,
            "leads_is_stale": True,
        }
    age_minutes = max(0, int((datetime.utcnow() - last_dt).total_seconds() // 60))
    return {
        "leads_last_seen_at": str(last),
        "leads_freshness_minutes": age_minutes,
        "leads_freshness_hours": age_minutes // 60,
        "leads_is_stale": age_minutes >= LEADS_STALE_HOURS * 60,
    }


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


def normalize_extracted_alert_job(raw_job, source_id: str, source_name: str) -> Job:
    if not isinstance(raw_job, dict):
        raise ServiceError(400, "Extracted job must be an object")
    title = clean_email_artifact(first_non_empty(raw_job, "title"))
    company = clean_email_artifact(first_non_empty(raw_job, "company"))
    url = first_non_empty(raw_job, "url")
    if not title:
        raise ServiceError(400, "Extracted job needs title")
    if not company:
        raise ServiceError(400, "Extracted job needs company")
    if not url:
        raise ServiceError(400, "Extracted job needs url")
    reject_noise_extracted_alert_job(title, company, url)
    try:
        validate_safe_url(url)
    except SourceError as exc:
        raise ServiceError(400, str(exc))
    snippet = safe_log_text(first_non_empty(raw_job, "snippet", "description"), 4000)
    location = safe_log_text(first_non_empty(raw_job, "location_hint", "location"), 300)
    return Job(
        source_id=source_id,
        source_name=source_name,
        external_id=url,
        url=url,
        title=title[:180],
        company=company[:180],
        location=location,
        remote_policy="unknown",
        description=snippet,
    )


def reject_noise_extracted_alert_job(title: str, company: str, url: str) -> None:
    if is_email_alert_noise_title(title):
        raise ServiceError(400, "Skipped email UI/noise title: %s" % safe_log_text(title, 80))
    if is_email_alert_placeholder_company(company):
        raise ServiceError(400, "Skipped email job without real company: %s" % safe_log_text(company, 80))
    if is_email_alert_noise_url(url):
        raise ServiceError(400, "Skipped email UI/profile/navigation URL")


def is_email_alert_noise_title(title: str) -> bool:
    text = normalize_email_alert_label(title)
    if text in EMAIL_ALERT_NOISE_TITLES:
        return True
    if text.startswith("message from "):
        return True
    return False


def is_email_alert_placeholder_company(company: str) -> bool:
    return normalize_email_alert_label(company) in EMAIL_ALERT_PLACEHOLDER_COMPANIES


def is_email_alert_noise_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    path_and_query = ("%s?%s" % (parsed.path or "", parsed.query or "")).lower()
    if re.search(r"\.(?:gif|jpe?g|png|webp|svg)(?:$|[?#])", path_and_query):
        return True
    if host.endswith("linkedin.com"):
        return not re.search(r"/(?:comm/)?jobs/view/\d+", path)
    if host.endswith("wellfound.com") or host.endswith("angel.co"):
        if re.fullmatch(r"/jobs/?", path or "/"):
            return True
    return any(term in path_and_query for term in EMAIL_ALERT_NAVIGATION_URL_TERMS)


def normalize_email_alert_label(value: str) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    text = re.sub(r"\s*[:.!-]+\s*$", "", text)
    return text


def clean_email_artifact(value: str) -> str:
    text = safe_log_text(value or "", 240)
    text = re.sub(r"\s+role\s+at\s+.+?\s+is available(?:\s+LinkedIn)?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+role$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+\|\s*LinkedIn$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+-\s*LinkedIn$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+is available(?:\s+LinkedIn)?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+LinkedIn$", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def job_from_row(row) -> Job:
    return Job(
        source_id=row["source_id"],
        source_name=row["source_name"],
        external_id=row["external_id"],
        url=row["url"],
        title=row["title"],
        company=row["company"],
        location=row["location"] or "",
        remote_policy=row["remote_policy"] or "unknown",
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        currency=row["currency"],
        description=row["description"] or "",
        posted_at=row["posted_at"],
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
    import re

    stripped = (sql or "").strip().lower()
    if not stripped:
        return False
    body = stripped[:-1].strip() if stripped.endswith(";") else stripped
    if ";" in body:
        return False
    if not (body.startswith("select ") or body.startswith("with ")):
        return False
    if body.startswith("with ") and not re.search(r"\bselect\b", body):
        return False
    blocked = (
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "pragma",
        "attach",
        "detach",
        "replace",
        "vacuum",
        "create",
        "reindex",
    )
    return re.search(r"\b(%s)\b" % "|".join(blocked), body) is None


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


def boolish(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
