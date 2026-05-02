import json
import logging
from pathlib import Path
from typing import Dict, List

from .agent_actions import sanitize_actions
from .config import AppConfig, load_sources
from .database import Database
from .logging_setup import log_context
from .models import UserProfile, utc_now_iso

LOGGER = logging.getLogger(__name__)


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
            "profile_md_full": self.profile.raw_text[:20000],
            "recent_directives_count": directive_count(self.profile.directives),
            "sources_summary": [source_summary(source) for source in load_sources(self.config.sources_path)],
            "recent_jobs_sample": [
                job_summary(row, self.config.agent_request_desc_chars)
                for row in self.database.recent_jobs(max(0, self.config.agent_request_recent_jobs))
            ],
            "recent_feedback_summary": recent_feedback_summary(
                self.database,
                max(0, self.config.agent_request_feedback_items),
                self.config.agent_request_desc_chars,
            ),
            "scoring_version": current_scoring_version(self.config.scoring_path),
            "instructions_hint": instructions_hint,
            "response_contract": {
                "user_intent_summary": "short text",
                "answer": "plain text shown to the user",
                "evidence_table": "optional rows/aggregates/file snippets/computed analyses for data_answer semantics",
                "proposed_actions": [
                    {
                        "kind": "directive_edit|profile_edit|sources_proposal|scoring_rule_proposal|data_answer|human_followup|rescore_jobs|bulk_update_jobs|backup_export",
                        "summary": "one-line user-facing summary",
                        "payload": {},
                    }
                ],
            },
        }
        write_json(request_path, payload)
        write_json(status_path, {"state": "pending", "updated_at": utc_now_iso(), "message": "Waiting for OpenClaw"})
        self.database.create_agent_run(session_id, user_text, str(request_path), str(status_path))
        log_context(LOGGER, logging.INFO, "agent_request_created", session_id=session_id, request_path=str(request_path))
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


def directive_count(text: str) -> int:
    return len([line for line in (text or "").splitlines() if line.strip()])


def source_summary(source) -> Dict:
    return {
        "id": source.id,
        "name": source.name,
        "type": source.type,
        "url": source.url,
        "status": source.status,
        "created_by": source.created_by,
        "query": source.query,
    }


def job_summary(row, desc_chars: int = 250) -> Dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "source_id": row["source_id"],
        "url": row["url"],
        "score": row["score"] if "score" in row.keys() else None,
        "status": row["status"],
        "description_excerpt": (row["description"] or "")[: max(0, desc_chars)],
    }


def recent_feedback_summary(database: Database, limit: int = 5, desc_chars: int = 250) -> Dict:
    return {
        "applied": [job_summary(row, desc_chars) for row in database.feedback_jobs("applied", limit)],
        "irrelevant": [job_summary(row, desc_chars) for row in database.feedback_jobs("irrelevant", limit)],
        "cover_note_requested": [job_summary(row, desc_chars) for row in database.feedback_jobs("cover_note", limit)],
    }


def current_scoring_version(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0) or 0)
    except Exception:
        return 0
