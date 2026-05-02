import difflib
import json
import logging
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import AppConfig, compose_profile, load_sources, split_profile_sections, validate_source_url
from .coordinators import validate_scoring_ruleset
from .logging_setup import log_context
from .models import SourceConfig, UserProfile, utc_now_iso
from .scoring import load_scoring_rules, score_job
from .sources import SourceError, validate_safe_url

LOGGER = logging.getLogger(__name__)

ALLOWED_ACTION_KINDS = {
    "directive_edit",
    "profile_edit",
    "sources_proposal",
    "scoring_rule_proposal",
    "data_answer",
    "human_followup",
    "rescore_jobs",
    "bulk_update_jobs",
    "backup_export",
}


@dataclass
class ActionResult:
    applied: bool
    message: str
    archive_path: str = ""
    target_path: str = ""
    requires_confirm: bool = False


@dataclass
class AgentActionContext:
    config: AppConfig
    database: object
    profile: UserProfile
    source_reachable: Optional[Callable[[str], bool]] = None
    shadow_test: Optional[Callable[[Dict], Dict]] = None
    run_l2: Optional[Callable[[List], None]] = None


def sanitize_actions(raw_actions: List[Dict]) -> List[Dict]:
    actions = []
    for raw in raw_actions or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", "")).strip()
        if kind not in ALLOWED_ACTION_KINDS:
            log_context(LOGGER, logging.WARNING, "agent_action_kind_dropped", kind=kind)
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        actions.append(
            {
                "kind": kind,
                "summary": sanitize_text(raw.get("summary") or default_summary(kind, payload), 300),
                "payload": payload,
            }
        )
    return actions


def apply_agent_action(action: Dict, context: AgentActionContext) -> ActionResult:
    kind = action.get("kind")
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    handler = KIND_HANDLERS.get(kind)
    if not handler:
        return ActionResult(False, "Unsupported action kind: %s" % kind)
    return handler(payload, context)


def directive_edit(payload: Dict, context: AgentActionContext) -> ActionResult:
    sections = split_profile_sections(read_text(context.config.profile_path))
    text = (
        payload.get("directive")
        or payload.get("append")
        or payload.get("new_directive")
        or payload.get("text")
        or payload.get("patch_diff")
        or ""
    )
    text = sanitize_text(text, 4000)
    if not text:
        return ActionResult(False, "directive_edit had no directive text")
    if "# About me" in text or "@@ # About me" in text:
        return ActionResult(False, "directive_edit refused because it touches # About me")
    archive = archive_file(context.config.profile_path)
    stamp = utc_now_iso()
    line = "[%s] %s" % (stamp[:10], text)
    directives = "\n".join(part for part in [sections["directives"], line] if part.strip())
    write_text(context.config.profile_path, compose_profile(sections["about_me"], directives))
    return ActionResult(True, "Directive added", str(archive), str(context.config.profile_path))


def profile_edit(payload: Dict, context: AgentActionContext) -> ActionResult:
    new_about = payload.get("new_about_me") or payload.get("about_me") or payload.get("text") or ""
    new_about = sanitize_text(new_about, 12000)
    if not new_about:
        return ActionResult(False, "profile_edit had no About me content")
    sections = split_profile_sections(read_text(context.config.profile_path))
    archive = archive_file(context.config.profile_path)
    write_text(context.config.profile_path, compose_profile(new_about, sections["directives"]))
    return ActionResult(True, "Profile About me replaced", str(archive), str(context.config.profile_path))


def sources_proposal(payload: Dict, context: AgentActionContext) -> ActionResult:
    operations = payload.get("operations") or []
    if not isinstance(operations, list):
        return ActionResult(False, "sources_proposal operations must be a list")
    disables = [op for op in operations if isinstance(op, dict) and op.get("op") == "disable"]
    if len(disables) > 5:
        return ActionResult(False, "Disabling more than 5 sources requires typed CONFIRM", requires_confirm=True)
    path = context.config.sources_path
    existing = json.loads(read_text(path) or "[]")
    by_id = {str(item.get("id")): item for item in existing if isinstance(item, dict)}
    by_url = {str(item.get("url")): item for item in existing if isinstance(item, dict)}
    archive = archive_file(path)
    changed = 0
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = operation.get("op")
        source = operation.get("source") if isinstance(operation.get("source"), dict) else {}
        if op == "add":
            row = normalize_source_row(source)
            if row["url"] in by_url or row["id"] in by_id:
                continue
            validate_source_row(row, context.source_reachable)
            existing.append(row)
            by_id[row["id"]] = row
            by_url[row["url"]] = row
            changed += 1
        elif op == "modify":
            target = by_id.get(str(source.get("id"))) or by_url.get(str(source.get("url")))
            if not target:
                continue
            updated = dict(target)
            for key in ("name", "type", "url", "status", "risk_level", "query", "priority"):
                if key in source:
                    updated[key] = source[key]
            validate_source_row(normalize_source_row(updated), context.source_reachable)
            target.update(normalize_source_row(updated))
            changed += 1
        elif op == "disable":
            target = by_id.get(str(source.get("id"))) or by_url.get(str(source.get("url")))
            if target:
                target["status"] = "disabled"
                target["enabled"] = False
                changed += 1
    if changed == 0:
        return ActionResult(False, "No source changes were applied", str(archive), str(path))
    write_text(path, json.dumps(existing, indent=2, sort_keys=True) + "\n")
    context.database.upsert_sources(load_sources(path))
    return ActionResult(True, "Applied %s source operation(s)" % changed, str(archive), str(path))


def scoring_rule_proposal(payload: Dict, context: AgentActionContext) -> ActionResult:
    proposed = payload.get("ruleset") or payload.get("proposed_rules") or payload
    if not isinstance(proposed, dict):
        return ActionResult(False, "scoring_rule_proposal ruleset must be an object")
    current = load_scoring_rules(context.config.scoring_path)
    current_version = int(current.get("version", 0) or 0)
    removed_count = max(0, len(current.get("rules", [])) - len(proposed.get("rules", [])))
    if removed_count > 5:
        return ActionResult(False, "Removing more than 5 scoring rules requires typed CONFIRM", requires_confirm=True)
    validate_scoring_ruleset(proposed, current_version)
    archive = archive_file(context.config.scoring_path)
    write_text(context.config.scoring_path, json.dumps(proposed, indent=2, sort_keys=True) + "\n")
    report = context.shadow_test(proposed) if context.shadow_test else {}
    context.database.create_scoring_version(int(proposed.get("version", current_version + 1)), str(context.config.scoring_path), report, status="active")
    return ActionResult(True, "Applied scoring version %s" % proposed.get("version"), str(archive), str(context.config.scoring_path))


def data_answer(payload: Dict, _context: AgentActionContext) -> ActionResult:
    answer = payload.get("answer") or payload.get("text") or "Read-only answer shown."
    return ActionResult(True, sanitize_text(answer, 3000))


def human_followup(payload: Dict, _context: AgentActionContext) -> ActionResult:
    tasks_path = Path.cwd() / "tasks.md"
    if not tasks_path.exists():
        return ActionResult(False, "tasks.md not found")
    title = sanitize_text(payload.get("title") or "Agent follow-up", 120)
    summary = sanitize_text(payload.get("summary") or payload.get("suggested_approach") or "", 700)
    archive = archive_file(tasks_path)
    next_id = next_task_id(tasks_path)
    row = "| %s | P3 | %s | %s | Pending agent-filed follow-up. | Filed from /agent for later implementation. |\n" % (
        next_id,
        title.replace("|", "/"),
        summary.replace("|", "/"),
    )
    with tasks_path.open("a", encoding="utf-8") as handle:
        handle.write(row)
    return ActionResult(True, "Filed task #%s: %s" % (next_id, title), str(archive), str(tasks_path))


def rescore_jobs(payload: Dict, context: AgentActionContext) -> ActionResult:
    hours = min(168, max(1, int(payload.get("window_hours", 24) or 24)))
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat() + "Z"
    rules = load_scoring_rules(context.config.scoring_path)
    rows = []
    with context.database.connection() as conn:
        rows = list(conn.execute("select * from jobs where last_seen_at >= ? order by last_seen_at desc", (cutoff,)))
    for row in rows:
        job = row_to_job(row)
        context.database.save_score(row["id"], score_job(job, context.profile, rules))
    if context.run_l2 and rows:
        context.run_l2(rows[: context.config.l2_max_jobs])
    return ActionResult(True, "Rescored %s job(s) from the last %sh" % (len(rows), hours))


def bulk_update_jobs(payload: Dict, context: AgentActionContext) -> ActionResult:
    filter_sql = str(payload.get("filter_sql") or "").strip()
    new_status = str(payload.get("new_status") or "").strip()
    if new_status not in ("archived", "rejected"):
        return ActionResult(False, "bulk_update_jobs only supports archived/rejected")
    if not is_select_only(filter_sql):
        return ActionResult(False, "bulk_update_jobs filter_sql must be a SELECT")
    with context.database.connection() as conn:
        rows = conn.execute(filter_sql).fetchall()
        job_ids = [row["id"] for row in rows if "id" in row.keys()][:100]
        if len(job_ids) > 10:
            return ActionResult(False, "Updating more than 10 jobs requires typed CONFIRM", requires_confirm=True)
        conn.executemany("update jobs set status = ? where id = ?", [(new_status, job_id) for job_id in job_ids])
    return ActionResult(True, "Updated %s job(s) to %s" % (len(job_ids), new_status))


def backup_export(payload: Dict, context: AgentActionContext) -> ActionResult:
    include = set(payload.get("include") or ["config", "input", "scoring_archives"])
    backup_dir = context.config.data_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out_path = backup_dir / ("jobhunter-%s.tar.gz" % datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    with tarfile.open(out_path, "w:gz") as tar:
        if "config" in include:
            tar.add(context.config.config_dir, arcname="config")
        if "input" in include:
            tar.add(context.config.input_dir, arcname="input")
        if "database" in include and context.config.database_path.exists():
            tar.add(context.config.database_path, arcname="data/jobs.sqlite")
    return ActionResult(True, "Backup created: %s (%s bytes)" % (out_path, out_path.stat().st_size), "", str(out_path))


KIND_HANDLERS = {
    "directive_edit": directive_edit,
    "profile_edit": profile_edit,
    "sources_proposal": sources_proposal,
    "scoring_rule_proposal": scoring_rule_proposal,
    "data_answer": data_answer,
    "human_followup": human_followup,
    "rescore_jobs": rescore_jobs,
    "bulk_update_jobs": bulk_update_jobs,
    "backup_export": backup_export,
}


def normalize_source_row(source: Dict) -> Dict:
    source_id = sanitize_id(source.get("id") or source.get("name") or source.get("url") or "agent-source")
    source_type = str(source.get("type") or "json_api").strip().lower()
    if source_type == "email_alert":
        source_type = "imap"
    row = {
        "id": source_id,
        "name": sanitize_text(source.get("name") or source_id, 80),
        "type": source_type,
        "url": sanitize_text(source.get("url") or ("imap://job-alerts" if source_type == "imap" else ""), 500),
        "status": source.get("status") or "test",
        "enabled": source.get("status") != "disabled",
        "risk_level": sanitize_text(source.get("risk_level") or source.get("risk") or "medium", 20),
        "created_by": source.get("created_by") or "agent",
    }
    if source.get("query"):
        row["query"] = sanitize_text(source.get("query"), 500)
    if source.get("priority") in ("high", "medium", "low"):
        row["priority"] = source.get("priority")
    return row


def validate_source_row(row: Dict, source_reachable: Optional[Callable[[str], bool]]) -> None:
    validate_source_url(row["url"], row["type"])
    if row["type"] != "imap":
        validate_safe_url(row["url"])
        if source_reachable and not source_reachable(row["url"]):
            raise SourceError("HEAD probe failed")


def archive_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    archive = path.with_name("%s.%s.bak" % (path.name, datetime.utcnow().strftime("%Y%m%d%H%M%S%f")))
    shutil.copyfile(path, archive)
    return archive


def row_to_job(row):
    from .models import Job

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


def next_task_id(path: Path) -> int:
    highest = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            parts = [part.strip() for part in line.strip("|").split("|")]
            if parts and parts[0].isdigit():
                highest = max(highest, int(parts[0]))
    return highest + 1


def is_select_only(sql: str) -> bool:
    lowered = sql.strip().lower()
    blocked = ("insert", "update", "delete", "drop", "alter", "pragma", "attach", "detach", "replace", "vacuum")
    return lowered.startswith("select ") and not any(("%s " % word) in lowered for word in blocked)


def default_summary(kind: str, payload: Dict) -> str:
    if kind == "directive_edit":
        return "Add or update profile directive"
    if kind == "profile_edit":
        return "Update profile About me"
    if kind == "sources_proposal":
        return "Apply source registry changes"
    if kind == "scoring_rule_proposal":
        return "Apply scoring ruleset"
    if kind == "human_followup":
        return str(payload.get("title") or "Create follow-up task")
    return kind


def sanitize_text(value, limit: int) -> str:
    text = "".join(char if char >= " " or char in "\n\t" else " " for char in str(value or ""))
    return text.strip()[:limit]


def sanitize_id(value) -> str:
    import re

    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text[:48] or "agent-source"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
