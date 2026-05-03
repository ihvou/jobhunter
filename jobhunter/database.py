import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .logging_setup import log_context
from .models import Job, ScoreResult, SourceConfig, utc_now_iso

LOGGER = logging.getLogger(__name__)
LATEST_SCHEMA_VERSION = 6


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                "create table if not exists schema_version (version integer primary key, applied_at text not null)"
            )
            current = self.current_schema_version(conn)
            if current < 1:
                migrate_v1(conn)
                set_schema_version(conn, 1)
            if current < 2:
                migrate_v2(conn)
                set_schema_version(conn, 2)
            if current < 3:
                migrate_v3(conn)
                set_schema_version(conn, 3)
            if current < 4:
                migrate_v4(conn)
                set_schema_version(conn, 4)
            if current < 5:
                migrate_v5(conn)
                set_schema_version(conn, 5)
            if current < 6:
                migrate_v6(conn)
                set_schema_version(conn, 6)
            trim_usage_logs(conn)
            log_context(LOGGER, logging.INFO, "database_initialized", path=str(self.path), version=LATEST_SCHEMA_VERSION)

    def current_schema_version(self, conn) -> int:
        row = conn.execute("select max(version) as version from schema_version").fetchone()
        return int(row["version"] or 0)

    def upsert_sources(self, sources: Iterable[SourceConfig]) -> None:
        with self.connection() as conn:
            for source in sources:
                conn.execute(
                    """
                    insert into sources (
                        id, name, type, url, risk_level, poll_frequency_minutes,
                        enabled, status, score, created_by, imap_last_uid, priority
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, 50, ?, ?, ?)
                    on conflict(id) do update set
                        name = excluded.name,
                        type = excluded.type,
                        url = excluded.url,
                        risk_level = excluded.risk_level,
                        poll_frequency_minutes = excluded.poll_frequency_minutes,
                        enabled = excluded.enabled,
                        status = excluded.status,
                        created_by = excluded.created_by,
                        priority = excluded.priority
                    """,
                    (
                        source.id,
                        source.name,
                        source.type,
                        source.url,
                        source.risk_level,
                        source.poll_frequency_minutes,
                        1 if source.enabled else 0,
                        source.status,
                        source.created_by,
                        source.imap_last_uid,
                        source.priority,
                    ),
                )

    def save_candidate_profile(self, raw_text: str, cv_text: str, parsed: Dict) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into candidate_profile (id, raw_text, cv_text, parsed_json, updated_at)
                values (1, ?, ?, ?, ?)
                on conflict(id) do update set
                    raw_text = excluded.raw_text,
                    cv_text = excluded.cv_text,
                    parsed_json = excluded.parsed_json,
                    updated_at = excluded.updated_at
                """,
                (raw_text, cv_text, json.dumps(parsed, sort_keys=True), utc_now_iso()),
            )

    def source_rows(self) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute("select * from sources order by id"))

    def source_imap_last_uid(self, source_id: str) -> int:
        with self.connection() as conn:
            row = conn.execute("select imap_last_uid from sources where id = ?", (source_id,)).fetchone()
            return int(row["imap_last_uid"] or 0) if row else 0

    def update_source_imap_uid(self, source_id: str, uid: int) -> None:
        with self.connection() as conn:
            conn.execute("update sources set imap_last_uid = max(coalesce(imap_last_uid, 0), ?) where id = ?", (uid, source_id))

    def start_source_run(self, source_id: str) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                "insert into source_runs (source_id, started_at) values (?, ?)",
                (source_id, utc_now_iso()),
            )
            return int(cursor.lastrowid)

    def finish_source_run(
        self,
        run_id: int,
        source_id: str,
        fetched_count: int,
        inserted_count: int,
        error: Optional[str] = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                update source_runs
                set finished_at = ?, fetched_count = ?, inserted_count = ?, error = ?
                where id = ?
                """,
                (utc_now_iso(), fetched_count, inserted_count, error, run_id),
            )
            conn.execute("update sources set last_run_at = ? where id = ?", (utc_now_iso(), source_id))

    def upsert_job(self, job: Job) -> Tuple[str, bool]:
        job_id = stable_job_id(job)
        now = utc_now_iso()
        normalized_title = normalize_key(job.title)
        normalized_company = normalize_key(job.company)
        with self.connection() as conn:
            existing = conn.execute("select id from jobs where id = ?", (job_id,)).fetchone()
            if existing:
                conn.execute(
                    """
                    update jobs
                    set last_seen_at = ?,
                        source_id = case when source_id = '' then ? else source_id end,
                        description = case when length(coalesce(description, '')) < length(coalesce(?, ''))
                                           then ? else description end,
                        normalized_title = ?,
                        normalized_company = ?
                    where id = ?
                    """,
                    (now, job.source_id, job.description, job.description, normalized_title, normalized_company, job_id),
                )
                return job_id, False
            duplicate = find_recent_duplicate(conn, normalized_title, normalized_company, job.posted_at, now)
            if duplicate:
                conn.execute(
                    """
                    update jobs
                    set last_seen_at = ?,
                        description = case when length(coalesce(description, '')) < length(coalesce(?, ''))
                                           then ? else description end
                    where id = ?
                    """,
                    (now, job.description, job.description, duplicate),
                )
                log_context(
                    LOGGER,
                    logging.INFO,
                    "job_secondary_duplicate",
                    job_id=duplicate,
                    source_id=job.source_id,
                    normalized_title=normalized_title,
                    normalized_company=normalized_company,
                )
                return duplicate, False
            conn.execute(
                """
                insert into jobs (
                    id, source_id, source_name, external_id, url, title, company,
                    location, remote_policy, salary_min, salary_max, currency,
                    description, posted_at, first_seen_at, last_seen_at, status,
                    normalized_title, normalized_company
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (
                    job_id,
                    job.source_id,
                    job.source_name,
                    job.external_id,
                    canonicalize_url(job.url),
                    job.title,
                    job.company,
                    job.location,
                    job.remote_policy,
                    job.salary_min,
                    job.salary_max,
                    job.currency,
                    job.description,
                    job.posted_at,
                    now,
                    now,
                    normalized_title,
                    normalized_company,
                ),
            )
            return job_id, True

    def save_score(self, job_id: str, score: ScoreResult) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into job_scores (
                    job_id, score, hard_reject, reasons_json, concerns_json,
                    breakdown_json, fired_rules_json, scored_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(job_id) do update set
                    score = excluded.score,
                    hard_reject = excluded.hard_reject,
                    reasons_json = excluded.reasons_json,
                    concerns_json = excluded.concerns_json,
                    breakdown_json = excluded.breakdown_json,
                    fired_rules_json = excluded.fired_rules_json,
                    scored_at = excluded.scored_at
                """,
                (
                    job_id,
                    score.score,
                    1 if score.hard_reject else 0,
                    json.dumps(score.reasons),
                    json.dumps(score.concerns),
                    json.dumps(score.breakdown),
                    json.dumps(score.fired_rules),
                    utc_now_iso(),
                ),
            )
            if score.hard_reject:
                conn.execute("update jobs set status = 'rejected' where id = ? and status = 'new'", (job_id,))

    def jobs_for_digest(self, limit: int, min_score: int = 0, include_l2_rejected: bool = False) -> List[sqlite3.Row]:
        now = utc_now_iso()
        l2_filter = "" if include_l2_rejected else "and (l2.verdict is null or l2.verdict in ('relevant', 'borderline'))"
        with self.connection() as conn:
            rows = list(
                conn.execute(
                    """
                    select j.*, s.score, s.reasons_json, s.concerns_json, s.fired_rules_json,
                           src.status as source_status,
                           l2.verdict as l2_verdict,
                           l2.priority as l2_priority,
                           l2.reason as l2_reason,
                           l2.evidence_json as l2_evidence_json
                    from jobs j
                    join job_scores s on s.job_id = j.id
                    left join sources src on src.id = j.source_id
                    left join job_l2_verdicts l2 on l2.job_id = j.id
                    where s.hard_reject = 0
                      and s.score >= ?
                      %s
                      and (
                        j.status = 'new'
                        or (j.status = 'snoozed' and j.snoozed_until <= ?)
                      )
                      and not exists (
                        select 1 from digest_log dl
                        where dl.job_id = j.id
                          and j.status != 'snoozed'
                      )
                    order by
                      case when j.status = 'new' then 0 else 1 end,
                      case l2.priority when 'high' then 3 when 'medium' then 2 when 'low' then 1 else 0 end desc,
                      s.score desc,
                      j.first_seen_at desc
                    limit ?
                    """
                    % l2_filter,
                    (min_score, now, limit),
                )
            )
            return rows

    def mark_digested(self, job_ids: List[str]) -> str:
        digest_id = hashlib.sha256(("|".join(job_ids) + utc_now_iso()).encode("utf-8")).hexdigest()[:16]
        if not job_ids:
            return digest_id
        with self.connection() as conn:
            now = utc_now_iso()
            conn.executemany(
                """
                insert or ignore into digest_log (digest_id, sent_at, job_id)
                values (?, ?, ?)
                """,
                [(digest_id, now, job_id) for job_id in job_ids],
            )
            conn.executemany(
                "update jobs set last_digest_at = ?, status = 'new', snoozed_until = null where id = ?",
                [(now, job_id) for job_id in job_ids],
            )
        return digest_id

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                """
                select j.*, s.score, s.reasons_json, s.concerns_json, src.status as source_status
                from jobs j
                left join job_scores s on s.job_id = j.id
                left join sources src on src.id = j.source_id
                where j.id = ?
                """,
                (job_id,),
            ).fetchone()

    def update_job_status(self, job_id: str, status: str, snoozed_until: Optional[str] = None) -> None:
        with self.connection() as conn:
            conn.execute("update jobs set status = ?, snoozed_until = ? where id = ?", (status, snoozed_until, job_id))

    def feedback_exists(self, job_id: str, action: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "select 1 from job_feedback where job_id = ? and action = ? limit 1",
                (job_id, action),
            ).fetchone()
            return row is not None

    def add_feedback(self, job_id: str, action: str, details: Optional[str] = None) -> bool:
        if job_id != "__system__" and action in ("applied", "irrelevant") and self.feedback_exists(job_id, action):
            return False
        with self.connection() as conn:
            conn.execute(
                "insert into job_feedback (job_id, action, details, created_at) values (?, ?, ?, ?)",
                (job_id, action, details, utc_now_iso()),
            )
            return True

    def save_l2_verdict(
        self,
        job_id: str,
        verdict: str,
        priority: str,
        reason: str,
        evidence: List[str],
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into job_l2_verdicts (
                    job_id, verdict, priority, reason, evidence_json, scored_at, model, input_tokens, output_tokens
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(job_id) do update set
                    verdict = excluded.verdict,
                    priority = excluded.priority,
                    reason = excluded.reason,
                    evidence_json = excluded.evidence_json,
                    scored_at = excluded.scored_at,
                    model = excluded.model,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens
                """,
                (
                    job_id,
                    verdict,
                    priority,
                    reason,
                    json.dumps(evidence or []),
                    utc_now_iso(),
                    model,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                ),
            )

    def latest_l2_verdict(self, job_id: str) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("select * from job_l2_verdicts where job_id = ?", (job_id,)).fetchone()

    def save_draft(self, job_id: str, draft_type: str, content: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "insert into drafts (job_id, draft_type, content, created_at) values (?, ?, ?, ?)",
                (job_id, draft_type, content, utc_now_iso()),
            )

    def log_usage(
        self,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into usage_log (
                    task, model, input_tokens, output_tokens, estimated_cost_usd, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (task, model, input_tokens, output_tokens, estimated_cost_usd, utc_now_iso()),
            )

    def spend_since(self, since: datetime) -> float:
        with self.connection() as conn:
            row = conn.execute(
                "select coalesce(sum(estimated_cost_usd), 0) as total from usage_log where created_at >= ?",
                (since.replace(microsecond=0).isoformat() + "Z",),
            ).fetchone()
            return float(row["total"])

    def count_since(self, table: str, since: datetime, where: str = "", params: Tuple = ()) -> int:
        with self.connection() as conn:
            sql = "select count(*) as total from %s where created_at >= ?" % table
            values = [since.replace(microsecond=0).isoformat() + "Z"]
            if where:
                sql += " and " + where
                values.extend(params)
            row = conn.execute(sql, tuple(values)).fetchone()
            return int(row["total"] or 0)

    def spend_today(self) -> float:
        now = datetime.utcnow()
        return self.spend_since(datetime(now.year, now.month, now.day))

    def spend_this_month(self) -> float:
        now = datetime.utcnow()
        return self.spend_since(datetime(now.year, now.month, 1))

    def usage_summary(self) -> Dict[str, float]:
        with self.connection() as conn:
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
            jobs_today = conn.execute("select count(*) as c from jobs where first_seen_at >= ?", (today,)).fetchone()["c"]
            cover_today = conn.execute(
                "select count(*) as c from usage_log where task = 'cover_note' and created_at >= ?", (today,)
            ).fetchone()["c"]
            last_discovery = conn.execute(
                "select requested_at from discovery_runs order by requested_at desc limit 1"
            ).fetchone()
            last_scoring = conn.execute(
                "select activated_at from scoring_versions order by activated_at desc limit 1"
            ).fetchone()
        return {
            "today": self.spend_today(),
            "month": self.spend_this_month(),
            "jobs_today": jobs_today,
            "cover_notes_today": cover_today,
            "last_discovery": last_discovery["requested_at"] if last_discovery else "",
            "last_scoring": last_scoring["activated_at"] if last_scoring else "",
        }

    def create_agent_run(self, session_id: str, user_text: str, request_path: str, status_path: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into agent_runs (session_id, user_text, requested_at, status, request_path, status_path)
                values (?, ?, ?, 'pending', ?, ?)
                """,
                (session_id, user_text, utc_now_iso(), request_path, status_path),
            )

    def update_agent_run(self, session_id: str, **fields) -> None:
        if not fields:
            return
        with self.connection() as conn:
            assignments = ", ".join("%s = ?" % key for key in fields)
            conn.execute("update agent_runs set %s where session_id = ?" % assignments, tuple(fields.values()) + (session_id,))

    def pending_agent_runs(self) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute("select * from agent_runs where status in ('pending', 'running') order by requested_at asc"))

    def active_agent_run(self) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                """
                select * from agent_runs
                where status in ('pending', 'running')
                order by requested_at asc
                limit 1
                """
            ).fetchone()

    def get_agent_run(self, session_id: str) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("select * from agent_runs where session_id = ?", (session_id,)).fetchone()

    def record_agent_action(
        self,
        session_id: str,
        kind: str,
        user_intent: str,
        summary: str,
        payload: Dict,
        status: str,
        archive_path: str = "",
        target_path: str = "",
        result_message: str = "",
        revert_target_id: Optional[int] = None,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                insert into agent_actions (
                    session_id, kind, user_intent, summary, diff_blob, payload_json,
                    applied_at, applied_by_user, status, archive_path, target_path,
                    result_message, revert_target_id
                ) values (?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    kind,
                    user_intent,
                    summary,
                    json.dumps(payload, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    utc_now_iso(),
                    status,
                    archive_path,
                    target_path,
                    result_message,
                    revert_target_id,
                ),
            )
            return int(cursor.lastrowid)

    def find_applied_agent_action(self, session_id: str, kind: str, payload: Dict) -> Optional[sqlite3.Row]:
        payload_json = json.dumps(payload, sort_keys=True)
        with self.connection() as conn:
            return conn.execute(
                """
                select * from agent_actions
                where session_id = ?
                  and kind = ?
                  and payload_json = ?
                  and status in ('applied', 'pending_confirm')
                order by id asc
                limit 1
                """,
                (session_id, kind, payload_json),
            ).fetchone()

    def recent_agent_actions(self, limit: int = 10) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute("select * from agent_actions order by id desc limit ?", (limit,)))

    def get_agent_action(self, action_id: int) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("select * from agent_actions where id = ?", (action_id,)).fetchone()

    def update_agent_action_status(self, action_id: int, status: str) -> None:
        with self.connection() as conn:
            conn.execute("update agent_actions set status = ? where id = ?", (status, action_id))

    def update_agent_action_result(
        self,
        action_id: int,
        status: str,
        archive_path: str = "",
        target_path: str = "",
        result_message: str = "",
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                update agent_actions
                set status = ?, archive_path = ?, target_path = ?, result_message = ?, applied_at = ?
                where id = ?
                """,
                (status, archive_path, target_path, result_message, utc_now_iso(), action_id),
            )

    def recent_jobs_by_status(self, status: str, limit: int = 10) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    select j.*, s.score, s.reasons_json, s.concerns_json, src.status as source_status,
                           l2.verdict as l2_verdict, l2.priority as l2_priority, l2.reason as l2_reason
                    from jobs j
                    left join job_scores s on s.job_id = j.id
                    left join sources src on src.id = j.source_id
                    left join job_l2_verdicts l2 on l2.job_id = j.id
                    where j.status = ?
                    order by j.last_seen_at desc
                    limit ?
                    """,
                    (status, limit),
                )
            )

    def feedback_jobs(self, action: str, limit: int = 50) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    select j.*, f.action, f.details, f.created_at as feedback_at,
                           s.score, s.fired_rules_json, l2.verdict as l2_verdict, l2.reason as l2_reason
                    from job_feedback f
                    join jobs j on j.id = f.job_id
                    left join job_scores s on s.job_id = j.id
                    left join job_l2_verdicts l2 on l2.job_id = j.id
                    where f.action = ?
                    order by f.created_at desc
                    limit ?
                    """,
                    (action, limit),
                )
            )

    def source_feedback_metrics(self) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    select
                        src.id,
                        src.score as current_score,
                        count(distinct j.id) as jobs_seen,
                        sum(case when f.action = 'irrelevant' then 1 else 0 end) as irrelevant_count,
                        sum(case when f.action = 'cover_note' then 1 else 0 end) as cover_note_count,
                        sum(case when f.action = 'applied' then 1 else 0 end) as applied_count
                    from sources src
                    left join jobs j on j.source_id = src.id
                    left join job_feedback f on f.job_id = j.id
                    group by src.id
                    """
                )
            )

    def update_source_score(self, source_id: str, score: int) -> None:
        with self.connection() as conn:
            conn.execute("update sources set score = ? where id = ?", (score, source_id))

    def promote_source_if_test(self, source_id: str) -> None:
        with self.connection() as conn:
            conn.execute("update sources set status = 'active', enabled = 1 where id = ? and status = 'test'", (source_id,))

    def rate_limit_check(self, action: str, seconds: int) -> Tuple[bool, int]:
        now = datetime.utcnow()
        with self.connection() as conn:
            row = conn.execute("select last_at from rate_limits where action = ?", (action,)).fetchone()
            if row and row["last_at"]:
                last = datetime.fromisoformat(row["last_at"].replace("Z", ""))
                elapsed = int((now - last).total_seconds())
                if elapsed < seconds:
                    return False, seconds - elapsed
            conn.execute(
                "insert into rate_limits (action, last_at, count_24h) values (?, ?, 1) "
                "on conflict(action) do update set last_at = excluded.last_at, count_24h = count_24h + 1",
                (action, utc_now_iso()),
            )
            return True, 0

    def rate_limit_daily(self, action: str, max_count: int) -> Tuple[bool, int]:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
        with self.connection() as conn:
            row = conn.execute("select count(*) as c from job_feedback where action = ? and created_at >= ?", (action, today)).fetchone()
            count = int(row["c"] or 0)
            if count >= max_count:
                return False, count
            return True, count

    def create_discovery_run(self, session_id: str, request_path: str, status_path: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into discovery_runs (
                    session_id, requested_at, status, request_path, status_path, candidate_count, approved_count
                ) values (?, ?, 'pending', ?, ?, 0, 0)
                """,
                (session_id, utc_now_iso(), request_path, status_path),
            )

    def update_discovery_run(self, session_id: str, **fields) -> None:
        if not fields:
            return
        with self.connection() as conn:
            assignments = ", ".join("%s = ?" % key for key in fields)
            conn.execute("update discovery_runs set %s where session_id = ?" % assignments, tuple(fields.values()) + (session_id,))

    def get_discovery_run(self, session_id: str) -> Optional[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("select * from discovery_runs where session_id = ?", (session_id,)).fetchone()

    def pending_discovery_runs(self) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute("select * from discovery_runs where status in ('pending', 'running')"))

    def create_scoring_version(self, version: int, rules_path: str, report_json: Dict, status: str = "pending") -> None:
        with self.connection() as conn:
            conn.execute(
                """
                insert into scoring_versions (version, generated_by, activated_at, rules_path, shadow_report_json, status)
                values (?, 'openclaw+codex', ?, ?, ?, ?)
                """,
                (version, utc_now_iso(), rules_path, json.dumps(report_json), status),
            )

    def recent_jobs(self, limit: int = 100) -> List[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    select j.*, s.score, s.hard_reject, s.reasons_json, s.concerns_json
                    from jobs j
                    left join job_scores s on s.job_id = j.id
                    order by j.first_seen_at desc
                    limit ?
                    """,
                    (limit,),
                )
            )

    def upsert_email_template(self, template: Dict) -> None:
        parser_config = template.get("parser_config") or {}
        parser_id = template.get("parser_config_id") or ("%s-parser" % template["id"])
        with self.connection() as conn:
            conn.execute(
                """
                insert into email_parser_configs (id, config_json, created_at)
                values (?, ?, ?)
                on conflict(id) do update set config_json = excluded.config_json
                """,
                (parser_id, json.dumps(parser_config, sort_keys=True), utc_now_iso()),
            )
            conn.execute(
                """
                insert into email_templates (
                    id, source_id, sender_pattern, subject_pattern, parser_config_id,
                    status, priority, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    source_id = excluded.source_id,
                    sender_pattern = excluded.sender_pattern,
                    subject_pattern = excluded.subject_pattern,
                    parser_config_id = excluded.parser_config_id,
                    status = excluded.status,
                    priority = excluded.priority
                """,
                (
                    template["id"],
                    template["source_id"],
                    template.get("sender_pattern", ".*"),
                    template.get("subject_pattern", ".*"),
                    parser_id,
                    template.get("status", "test"),
                    template.get("priority", "medium"),
                    utc_now_iso(),
                ),
            )

    def email_templates_for_source(self, source_id: str) -> List[Dict]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                select t.*, c.config_json
                from email_templates t
                join email_parser_configs c on c.id = t.parser_config_id
                where t.source_id = ? and t.status != 'disabled'
                order by case t.priority when 'high' then 0 when 'medium' then 1 else 2 end, t.id
                """,
                (source_id,),
            ).fetchall()
        templates = []
        for row in rows:
            templates.append(
                {
                    "id": row["id"],
                    "source_id": row["source_id"],
                    "sender_pattern": row["sender_pattern"],
                    "subject_pattern": row["subject_pattern"],
                    "parser_config": json.loads(row["config_json"] or "{}"),
                    "status": row["status"],
                    "priority": row["priority"],
                }
            )
        return templates


def migrate_v1(conn) -> None:
    conn.executescript(
        """
        create table if not exists candidate_profile (
            id integer primary key check (id = 1),
            raw_text text,
            cv_text text,
            parsed_json text,
            updated_at text
        );

        create table if not exists sources (
            id text primary key,
            name text not null,
            type text not null,
            url text not null,
            risk_level text not null,
            poll_frequency_minutes integer not null default 360,
            enabled integer not null default 1,
            status text not null default 'active',
            score integer not null default 50,
            created_by text not null default 'user',
            imap_last_uid integer not null default 0,
            last_run_at text
        );

        create table if not exists source_runs (
            id integer primary key autoincrement,
            source_id text not null,
            started_at text not null,
            finished_at text,
            fetched_count integer not null default 0,
            inserted_count integer not null default 0,
            error text
        );

        create table if not exists jobs (
            id text primary key,
            source_id text not null,
            source_name text not null,
            external_id text,
            url text not null,
            title text not null,
            company text not null,
            location text,
            remote_policy text,
            salary_min integer,
            salary_max integer,
            currency text,
            description text,
            posted_at text,
            first_seen_at text not null,
            last_seen_at text not null,
            last_digest_at text,
            snoozed_until text,
            status text not null default 'new'
        );

        create table if not exists job_scores (
            job_id text primary key,
            score integer not null,
            hard_reject integer not null,
            reasons_json text not null,
            concerns_json text not null,
            breakdown_json text not null,
            fired_rules_json text not null default '[]',
            scored_at text not null
        );

        create table if not exists job_feedback (
            id integer primary key autoincrement,
            job_id text not null,
            action text not null,
            details text,
            created_at text not null
        );

        create table if not exists drafts (
            id integer primary key autoincrement,
            job_id text not null,
            draft_type text not null,
            content text not null,
            created_at text not null
        );

        create table if not exists usage_log (
            id integer primary key autoincrement,
            task text not null,
            model text not null,
            input_tokens integer not null,
            output_tokens integer not null,
            estimated_cost_usd real not null,
            created_at text not null
        );
        """
    )


def migrate_v2(conn) -> None:
    for column_sql in [
        "alter table sources add column status text not null default 'active'",
        "alter table sources add column imap_last_uid integer not null default 0",
        "alter table sources add column created_by text not null default 'user'",
        "alter table job_scores add column fired_rules_json text not null default '[]'",
    ]:
        try:
            conn.execute(column_sql)
        except sqlite3.OperationalError:
            pass
    conn.executescript(
        """
        drop table if exists experiments;

        create table if not exists digest_log (
            digest_id text not null,
            sent_at text not null,
            job_id text not null,
            primary key (digest_id, job_id)
        );
        create index if not exists idx_digest_log_job_id on digest_log(job_id);

        create table if not exists discovery_runs (
            session_id text primary key,
            requested_at text not null,
            status text not null,
            request_path text,
            status_path text,
            response_path text,
            candidate_count integer not null default 0,
            approved_count integer not null default 0,
            message text
        );

        create table if not exists scoring_versions (
            id integer primary key autoincrement,
            version integer not null,
            generated_by text not null,
            activated_at text,
            rules_path text not null,
            shadow_report_json text not null,
            status text not null default 'pending'
        );

        create table if not exists rate_limits (
            action text primary key,
            last_at text not null,
            count_24h integer not null default 0
        );

        create table if not exists usage_daily (
            day text primary key,
            estimated_cost_usd real not null,
            input_tokens integer not null,
            output_tokens integer not null
        );
        """
    )


def migrate_v3(conn) -> None:
    for column_sql in [
        "alter table jobs add column normalized_title text not null default ''",
        "alter table jobs add column normalized_company text not null default ''",
    ]:
        try:
            conn.execute(column_sql)
        except sqlite3.OperationalError:
            pass
    rows = conn.execute("select id, title, company from jobs").fetchall()
    for row in rows:
        conn.execute(
            "update jobs set normalized_title = ?, normalized_company = ? where id = ?",
            (normalize_key(row["title"]), normalize_key(row["company"]), row["id"]),
        )
    conn.execute("create index if not exists idx_jobs_normalized_title_company on jobs(normalized_title, normalized_company)")


def migrate_v4(conn) -> None:
    try:
        conn.execute("alter table sources add column priority text not null default 'medium'")
    except sqlite3.OperationalError:
        pass
    conn.executescript(
        """
        create table if not exists job_l2_verdicts (
            job_id text primary key,
            verdict text not null,
            priority text not null,
            reason text not null,
            evidence_json text not null default '[]',
            scored_at text not null,
            model text not null,
            input_tokens integer not null default 0,
            output_tokens integer not null default 0
        );

        create table if not exists agent_runs (
            session_id text primary key,
            user_text text not null,
            requested_at text not null,
            status text not null,
            request_path text,
            status_path text,
            response_path text,
            message text
        );

        create table if not exists agent_actions (
            id integer primary key autoincrement,
            session_id text not null,
            kind text not null,
            user_intent text,
            summary text,
            diff_blob text,
            payload_json text,
            applied_at text not null,
            applied_by_user text,
            status text not null,
            archive_path text,
            target_path text,
            result_message text,
            revert_target_id integer
        );
        create index if not exists idx_agent_actions_session on agent_actions(session_id);
        create index if not exists idx_agent_actions_status on agent_actions(status);
        """
    )


def migrate_v5(conn) -> None:
    conn.executescript(
        """
        create table if not exists email_parser_configs (
            id text primary key,
            config_json text not null,
            created_at text not null
        );

        create table if not exists email_templates (
            id text primary key,
            source_id text not null,
            sender_pattern text not null,
            subject_pattern text not null,
            parser_config_id text not null,
            status text not null default 'test',
            priority text not null default 'medium',
            created_at text not null
        );
        create index if not exists idx_email_templates_source on email_templates(source_id, status);
        """
    )


def migrate_v6(conn) -> None:
    try:
        conn.execute("alter table agent_runs add column placeholder_message_id integer")
    except sqlite3.OperationalError:
        pass


def set_schema_version(conn, version: int) -> None:
    conn.execute("insert or ignore into schema_version (version, applied_at) values (?, ?)", (version, utc_now_iso()))


def trim_usage_logs(conn) -> None:
    cutoff = (datetime.utcnow() - timedelta(days=90)).replace(microsecond=0).isoformat() + "Z"
    rows = conn.execute(
        """
        select substr(created_at, 1, 10) as day,
               sum(estimated_cost_usd) as cost,
               sum(input_tokens) as input_tokens,
               sum(output_tokens) as output_tokens
        from usage_log
        where created_at < ?
        group by substr(created_at, 1, 10)
        """,
        (cutoff,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            insert into usage_daily (day, estimated_cost_usd, input_tokens, output_tokens)
            values (?, ?, ?, ?)
            on conflict(day) do update set
              estimated_cost_usd = excluded.estimated_cost_usd,
              input_tokens = excluded.input_tokens,
              output_tokens = excluded.output_tokens
            """,
            (row["day"], row["cost"] or 0, row["input_tokens"] or 0, row["output_tokens"] or 0),
        )
    conn.execute("delete from usage_log where created_at < ?", (cutoff,))


def stable_job_id(job: Job) -> str:
    basis = "|".join([canonicalize_url(job.url), normalize_key(job.company), normalize_key(job.title)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url or "")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in ("ref", "source", "fbclid", "gclid")
    ]
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", urlencode(query), ""))


def normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def find_recent_duplicate(conn, normalized_title: str, normalized_company: str, posted_at: Optional[str], now: str) -> Optional[str]:
    if not normalized_title or not normalized_company:
        return None
    rows = conn.execute(
        """
        select id, posted_at, first_seen_at
        from jobs
        where normalized_title = ? and normalized_company = ?
        order by first_seen_at desc
        limit 25
        """,
        (normalized_title, normalized_company),
    ).fetchall()
    candidate_date = dedupe_date(posted_at, now)
    for row in rows:
        row_date = dedupe_date(row["posted_at"], row["first_seen_at"])
        if candidate_date and row_date and abs((candidate_date - row_date).days) <= 7:
            return row["id"]
    return None


def dedupe_date(posted_at: Optional[str], fallback: Optional[str]) -> Optional[datetime]:
    for value in (posted_at, fallback):
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def tomorrow_iso() -> str:
    return (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat() + "Z"
