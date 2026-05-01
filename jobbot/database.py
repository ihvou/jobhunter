import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Job, ScoreResult, SourceConfig, utc_now_iso


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists sources (
                    id text primary key,
                    name text not null,
                    type text not null,
                    url text not null,
                    risk_level text not null,
                    poll_frequency_minutes integer not null,
                    enabled integer not null,
                    score integer not null default 50,
                    created_by text not null default 'seed',
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

                create table if not exists experiments (
                    id integer primary key autoincrement,
                    source_id text,
                    query text,
                    status text not null,
                    rationale text,
                    created_at text not null
                );
                """
            )

    def upsert_sources(self, sources: Iterable[SourceConfig]) -> None:
        with self.connect() as conn:
            for source in sources:
                conn.execute(
                    """
                    insert into sources (
                        id, name, type, url, risk_level, poll_frequency_minutes, enabled
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(id) do update set
                        name = excluded.name,
                        type = excluded.type,
                        url = excluded.url,
                        risk_level = excluded.risk_level,
                        poll_frequency_minutes = excluded.poll_frequency_minutes,
                        enabled = excluded.enabled
                    """,
                    (
                        source.id,
                        source.name,
                        source.type,
                        source.url,
                        source.risk_level,
                        source.poll_frequency_minutes,
                        1 if source.enabled else 0,
                    ),
                )

    def start_source_run(self, source_id: str) -> int:
        with self.connect() as conn:
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
        with self.connect() as conn:
            conn.execute(
                """
                update source_runs
                set finished_at = ?, fetched_count = ?, inserted_count = ?, error = ?
                where id = ?
                """,
                (utc_now_iso(), fetched_count, inserted_count, error, run_id),
            )
            conn.execute(
                "update sources set last_run_at = ? where id = ?",
                (utc_now_iso(), source_id),
            )

    def upsert_job(self, job: Job) -> Tuple[str, bool]:
        job_id = stable_job_id(job)
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute("select id from jobs where id = ?", (job_id,)).fetchone()
            if existing:
                conn.execute(
                    """
                    update jobs
                    set last_seen_at = ?,
                        description = case when length(coalesce(description, '')) < length(coalesce(?, ''))
                                           then ? else description end
                    where id = ?
                    """,
                    (now, job.description, job.description, job_id),
                )
                return job_id, False
            conn.execute(
                """
                insert into jobs (
                    id, source_id, source_name, external_id, url, title, company,
                    location, remote_policy, salary_min, salary_max, currency,
                    description, posted_at, first_seen_at, last_seen_at, status
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    job_id,
                    job.source_id,
                    job.source_name,
                    job.external_id,
                    job.url,
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
                ),
            )
            return job_id, True

    def save_score(self, job_id: str, score: ScoreResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into job_scores (
                    job_id, score, hard_reject, reasons_json, concerns_json,
                    breakdown_json, scored_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(job_id) do update set
                    score = excluded.score,
                    hard_reject = excluded.hard_reject,
                    reasons_json = excluded.reasons_json,
                    concerns_json = excluded.concerns_json,
                    breakdown_json = excluded.breakdown_json,
                    scored_at = excluded.scored_at
                """,
                (
                    job_id,
                    score.score,
                    1 if score.hard_reject else 0,
                    json.dumps(score.reasons),
                    json.dumps(score.concerns),
                    json.dumps(score.breakdown),
                    utc_now_iso(),
                ),
            )
            if score.hard_reject:
                conn.execute(
                    "update jobs set status = 'rejected' where id = ? and status = 'new'",
                    (job_id,),
                )

    def jobs_for_digest(self, limit: int) -> List[sqlite3.Row]:
        now = utc_now_iso()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    select j.*, s.score, s.reasons_json, s.concerns_json
                    from jobs j
                    join job_scores s on s.job_id = j.id
                    where s.hard_reject = 0
                      and (
                        j.status = 'new'
                        or (j.status = 'snoozed' and j.snoozed_until <= ?)
                      )
                    order by s.score desc, j.first_seen_at desc
                    limit ?
                    """,
                    (now, limit),
                )
            )

    def mark_digested(self, job_ids: List[str]) -> None:
        if not job_ids:
            return
        with self.connect() as conn:
            conn.executemany(
                "update jobs set last_digest_at = ? where id = ?",
                [(utc_now_iso(), job_id) for job_id in job_ids],
            )

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                select j.*, s.score, s.reasons_json, s.concerns_json
                from jobs j
                left join job_scores s on s.job_id = j.id
                where j.id = ?
                """,
                (job_id,),
            ).fetchone()

    def update_job_status(self, job_id: str, status: str, snoozed_until: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update jobs set status = ?, snoozed_until = ? where id = ?",
                (status, snoozed_until, job_id),
            )

    def add_feedback(self, job_id: str, action: str, details: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into job_feedback (job_id, action, details, created_at) values (?, ?, ?, ?)",
                (job_id, action, details, utc_now_iso()),
            )

    def save_draft(self, job_id: str, draft_type: str, content: str) -> None:
        with self.connect() as conn:
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
        with self.connect() as conn:
            conn.execute(
                """
                insert into usage_log (
                    task, model, input_tokens, output_tokens, estimated_cost_usd, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (task, model, input_tokens, output_tokens, estimated_cost_usd, utc_now_iso()),
            )

    def spend_since(self, since: datetime) -> float:
        with self.connect() as conn:
            row = conn.execute(
                "select coalesce(sum(estimated_cost_usd), 0) as total from usage_log where created_at >= ?",
                (since.replace(microsecond=0).isoformat() + "Z",),
            ).fetchone()
            return float(row["total"])

    def spend_today(self) -> float:
        now = datetime.utcnow()
        start = datetime(now.year, now.month, now.day)
        return self.spend_since(start)

    def spend_this_month(self) -> float:
        now = datetime.utcnow()
        start = datetime(now.year, now.month, 1)
        return self.spend_since(start)

    def usage_summary(self) -> Dict[str, float]:
        return {
            "today": self.spend_today(),
            "month": self.spend_this_month(),
        }

    def source_feedback_metrics(self) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    select
                        src.id,
                        src.score as current_score,
                        count(j.id) as jobs_seen,
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
        with self.connect() as conn:
            conn.execute("update sources set score = ? where id = ?", (score, source_id))


def stable_job_id(job: Job) -> str:
    basis = "|".join(
        [
            job.source_id or "",
            job.external_id or "",
            normalize_key(job.url),
            normalize_key(job.company),
            normalize_key(job.title),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def tomorrow_iso() -> str:
    return (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat() + "Z"

