import json
import logging
from pathlib import Path
from typing import Dict, List

from .agent_actions import sanitize_actions
from .config import AppConfig
from .database import Database
from .logging_setup import log_context, safe_log_text
from .models import UserProfile, utc_now_iso

LOGGER = logging.getLogger(__name__)


RECENT_AGENT_RUN_LIMIT = 5
RECENT_AGENT_ACTION_LIMIT = 10


AVAILABLE_FILES = [
    "input/profile.local.md",
    "input/cv.local.md",
    "config/sources.json",
    "config/scoring.json",
    "config/jobhunter.json",
    "data/email_samples",
    "jobhunter/database.py",
    "jobhunter/agent_actions.py",
    "jobhunter/sources.py",
    "jobhunter/scoring.py",
    "jobhunter/coordinators.py",
    "jobhunter/app.py",
    "openclaw/prompts/agent.md",
    "openclaw/prompts/discovery.md",
    "openclaw/prompts/tuning.md",
    "tasks.md",
    "ARCHITECTURE.md",
]

DB_TABLES = [
    "jobs",
    "sources",
    "job_scores",
    "job_feedback",
    "job_l2_verdicts",
    "digest_log",
    "source_runs",
    "scoring_versions",
    "discovery_runs",
    "agent_runs",
    "agent_actions",
    "usage_log",
    "usage_daily",
    "drafts",
    "email_templates",
    "email_parser_configs",
]


class AgentCoordinator:
    def __init__(self, config: AppConfig, database: Database, profile: UserProfile):
        self.config = config
        self.database = database
        self.profile = profile

    @property
    def directory(self) -> Path:
        return self.config.workspace_dir / "agent"

    def create_request(self, user_text: str, instructions_hint: str = "") -> str:
        session_id = timestamp_id()
        request_path = self.directory / ("request-%s.json" % session_id)
        status_path = self.directory / ("status-%s.json" % session_id)
        payload = {
            "session_id": session_id,
            "user_text": user_text,
            "instructions_hint": instructions_hint,
            "available_files": AVAILABLE_FILES,
            "db_tables": DB_TABLES,
            "counts": agent_counts(self.database),
            "recent_agent_runs": recent_agent_runs_summary(self.database, RECENT_AGENT_RUN_LIMIT),
            "recent_actions_summary": recent_actions_summary(self.database, RECENT_AGENT_ACTION_LIMIT),
            "scoring_version": current_scoring_version(self.config.scoring_path),
            "note": "This payload is metadata only. recent_agent_runs/recent_actions_summary are compact memory hints, not source data. All real data lives in the files above and the SQLite DB at /jobhunter/data/jobs.sqlite. Use read_file / list_dir / query_sql / http_fetch on turn 1 to fetch what you need; do not answer from training memory.",
            "response_contract": {
                "user_intent_summary": "short text",
                "answer": "plain text shown to the user",
                "evidence_table": "optional rows/aggregates/file snippets/computed analyses for data_answer semantics",
                "proposed_actions": [
                    {
                        "kind": "directive_edit|profile_edit|sources_proposal|scoring_rule_proposal|data_answer|human_followup|rescore_jobs|bulk_update_jobs|backup_export|email_parser_proposal",
                        "summary": "one-line user-facing summary",
                        "payload": {},
                    }
                ],
            },
        }
        write_json(request_path, payload)
        write_json(status_path, {"state": "pending", "updated_at": utc_now_iso(), "message": "Waiting for OpenClaw"})
        self.database.create_agent_run(session_id, user_text, str(request_path), str(status_path))
        log_context(
            LOGGER,
            logging.INFO,
            "agent_request_created",
            session_id=session_id,
            request_path=str(request_path),
            user_text=safe_log_text(user_text, 200),
        )
        return session_id

    def poll_done(self) -> List[Dict]:
        completed = []
        for row in self.database.pending_agent_runs():
            status_path = Path(row["status_path"])
            if not status_path.exists():
                continue
            status = read_json(status_path)
            state = str(status.get("state") or "")
            session_id = row["session_id"]
            if state == "failed":
                self.database.update_agent_run(session_id, status="failed", message=status.get("message", "failed"))
                completed.append({"session_id": session_id, "error": status.get("message", "failed")})
                continue
            if state != "done":
                continue
            response_path = self.directory / ("response-%s.json" % session_id)
            if not response_path.exists():
                continue
            response = normalize_agent_response(read_json(response_path))
            self.database.update_agent_run(
                session_id,
                status="done",
                response_path=str(response_path),
                message=response.get("answer", "")[:500],
            )
            completed.append({"session_id": session_id, "response_path": str(response_path), "response": response})
        return completed


def normalize_agent_response(raw: Dict) -> Dict:
    if not isinstance(raw, dict):
        raw = {}
    actions = sanitize_actions(raw.get("proposed_actions") or [])
    return {
        "user_intent_summary": str(raw.get("user_intent_summary") or "Agent request"),
        "answer": str(raw.get("answer") or "I prepared a response."),
        "evidence_table": raw.get("evidence_table"),
        "proposed_actions": actions,
        "usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
    }


def read_agent_response(config: AppConfig, session_id: str) -> Dict:
    return normalize_agent_response(read_json(config.workspace_dir / "agent" / ("response-%s.json" % session_id)))


def write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def timestamp_id() -> str:
    from datetime import datetime

    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def agent_counts(database: Database) -> Dict:
    try:
        with database.connection() as conn:
            counts = {
                "sources_total": conn.execute("select count(*) as c from sources").fetchone()["c"],
                "sources_active": conn.execute("select count(*) as c from sources where status = 'active'").fetchone()["c"],
                "jobs_total": conn.execute("select count(*) as c from jobs").fetchone()["c"],
                "jobs_new": conn.execute("select count(*) as c from jobs where status = 'new'").fetchone()["c"],
                "applied": conn.execute("select count(*) as c from job_feedback where action = 'applied'").fetchone()["c"],
                "irrelevant": conn.execute("select count(*) as c from job_feedback where action = 'irrelevant'").fetchone()["c"],
                "cover_notes": conn.execute("select count(*) as c from job_feedback where action = 'cover_note'").fetchone()["c"],
            }
            last_digest = conn.execute("select max(sent_at) as t from digest_log").fetchone()
            counts["last_digest_at"] = last_digest["t"] if last_digest and last_digest["t"] else ""
            return counts
    except Exception as exc:
        LOGGER.warning("agent_counts_failed: %s", exc)
        return {}


def recent_agent_runs_summary(database: Database, limit: int = RECENT_AGENT_RUN_LIMIT) -> List[Dict]:
    rows = database.recent_agent_runs(limit)
    summaries = []
    for row in rows:
        response = read_optional_json(row["response_path"]) if row["response_path"] else {}
        proposed = response.get("proposed_actions") if isinstance(response.get("proposed_actions"), list) else []
        proposed_kinds = []
        for action in proposed:
            if isinstance(action, dict) and action.get("kind"):
                proposed_kinds.append(str(action.get("kind"))[:80])
        summaries.append(
            {
                "session_id": row["session_id"],
                "asked_at": row["requested_at"],
                "status": row["status"],
                "user_text": safe_log_text(row["user_text"], 220),
                "answer_excerpt": safe_log_text(response.get("answer") or row["message"], 300),
                "proposed_action_kinds": proposed_kinds[:10],
                "applied_action_count": int(row["applied_action_count"] or 0),
            }
        )
    return summaries


def recent_actions_summary(database: Database, limit: int = RECENT_AGENT_ACTION_LIMIT) -> List[Dict]:
    summaries = []
    for row in database.recent_agent_actions(limit):
        summaries.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "applied_at": row["applied_at"],
                "status": row["status"],
                "result_message_excerpt": safe_log_text(row["result_message"], 220),
            }
        )
    return summaries


def read_optional_json(path: str) -> Dict:
    try:
        if not path:
            return {}
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("agent_response_memory_read_failed: %s", exc)
        return {}


def current_scoring_version(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0) or 0)
    except Exception:
        return 0
