import atexit
import json
import logging
import signal
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .budget import BudgetGate
from .agent_actions import AgentActionContext, apply_agent_action
from .config import (
    AppConfig,
    ensure_directories,
    ensure_profile_file,
    load_app_config,
    load_profile,
    load_sources,
    validate_source_url,
)
from .coordinators import ScoringCoordinator
from .database import Database
from .llm import BudgetExceeded, LLMClient, LLMError
from .logging_setup import configure_logging, log_context, safe_log_text
from .models import Job, SourceConfig, utc_now_iso
from .scoring import load_scoring_rules, score_job
from . import sources as source_module
from .sources import SourceError, collect_from_source, normalize_source_type, validate_safe_url

LOGGER = logging.getLogger(__name__)


class JobHunter:
    """Headless Jobhunter domain service.

    OpenClaw owns Telegram, Codex sessions, inline buttons, and user-visible
    messaging. This class keeps deterministic local behavior: source
    collection, scoring, L2 relevance, action side effects, and audit data.
    """

    def __init__(self, config: AppConfig):
        configure_logging()
        self.config = config
        ensure_directories(config)
        ensure_profile_file(config)
        self.database = Database(config.database_path)
        self.database.init_schema()
        self.profile = load_profile(config)
        self.budget = BudgetGate(config, self.database)
        self.llm = LLMClient(config, self.budget)
        source_module.MAX_BYTES = config.max_response_bytes
        source_module.CHECK_ROBOTS = config.check_robots
        source_module.ROBOTS_TXT_RESPECT = config.robots_txt_respect
        self.scoring = ScoringCoordinator(config, self.database, self.profile)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.futures = set()
        self.shutdown_requested = False
        atexit.register(self.shutdown_executor)
        try:
            signal.signal(signal.SIGTERM, self.handle_shutdown_signal)
            signal.signal(signal.SIGINT, self.handle_shutdown_signal)
        except ValueError:
            log_context(LOGGER, logging.DEBUG, "shutdown_signal_handler_skipped")

    @classmethod
    def from_environment(cls):
        return cls(load_app_config())

    def initialize(self) -> None:
        sources = load_sources(self.config.sources_path)
        self.database.upsert_sources(sources)
        self.database.save_candidate_profile(
            self.profile.raw_text,
            self.profile.cv_text,
            {
                "target_titles": self.profile.target_titles,
                "positive_keywords": self.profile.positive_keywords,
                "negative_keywords": self.profile.negative_keywords,
                "required_locations": self.profile.required_locations,
                "excluded_locations": self.profile.excluded_locations,
                "excluded_domains": self.profile.excluded_domains,
                "salary_floor": self.profile.salary_floor,
                "currency": self.profile.currency,
            },
        )
        self.touch_heartbeat()
        log_context(LOGGER, logging.INFO, "jobhunter_initialized", sources=len(sources), database=str(self.config.database_path))
        print("Initialized jobhunter with %s sources and database %s" % (len(sources), self.config.database_path))

    def collect(self) -> None:
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        sources = sorted(
            [source for source in load_sources(self.config.sources_path) if source.enabled],
            key=lambda source: (priority_rank.get(source.priority, 1), source.id),
        )
        for source in sources:
            source.imap_last_uid = self.database.source_imap_last_uid(source.id)
            if source.type == "imap":
                source.email_templates = self.database.email_templates_for_source(source.id)
        self.database.upsert_sources(sources)
        ruleset = load_scoring_rules(self.config.scoring_path)
        total_fetched = 0
        total_inserted = 0
        log_context(LOGGER, logging.INFO, "collection_started", sources=len(sources))
        for source in sources:
            fetched, inserted = self.collect_source(source, ruleset)
            total_fetched += fetched
            total_inserted += inserted
            if source.type == "imap" and source.last_seen_uid:
                self.database.update_source_imap_uid(source.id, source.last_seen_uid)
        self.recalculate_source_scores()
        log_context(LOGGER, logging.INFO, "collection_completed", fetched=total_fetched, inserted=total_inserted)
        print("Collection complete: fetched=%s inserted=%s" % (total_fetched, total_inserted))

    def collect_source(self, source: SourceConfig, ruleset):
        run_id = self.database.start_source_run(source.id)
        fetched_count = 0
        inserted_count = 0
        error = None
        l2_candidates = []
        try:
            if self.shutdown_requested:
                raise SourceError("interrupted")
            jobs = collect_from_source(source)
            fetched_count = len(jobs)
            for job in jobs:
                if self.shutdown_requested:
                    raise SourceError("interrupted")
                job_id, inserted = self.database.upsert_job(job)
                if inserted:
                    inserted_count += 1
                result = score_job(job, self.profile, ruleset)
                self.database.save_score(job_id, result)
                if inserted and self.should_l2_score(source, job, result.score, len(l2_candidates)):
                    row = self.database.get_job(job_id)
                    if row:
                        l2_candidates.append(row)
            if l2_candidates:
                self.run_l2_relevance(l2_candidates)
            log_context(LOGGER, logging.INFO, "source_collected", source_id=source.id, fetched=fetched_count, inserted=inserted_count)
        except SourceError as exc:
            error = str(exc)
            log_context(LOGGER, logging.WARNING, "source_error", source_id=source.id, error=error)
            self.warn_agent_source_collection_failure(source, error)
        except Exception as exc:
            error = "%s: %s" % (exc.__class__.__name__, exc)
            log_context(LOGGER, logging.ERROR, "source_unexpected_error", source_id=source.id, error=error)
            self.warn_agent_source_collection_failure(source, error)
        finally:
            if self.shutdown_requested and error is None:
                error = "interrupted"
            self.database.finish_source_run(run_id, source.id, fetched_count, inserted_count, error)
        return fetched_count, inserted_count

    def should_l2_score(self, source: SourceConfig, job: Job, l1_score: int, current_count: int) -> bool:
        if current_count >= self.config.l2_max_jobs:
            return False
        if l1_score >= 40:
            return True
        title = (job.title or "").lower()
        description = (job.description or "").lower()
        strong_terms = ("product manager", "product owner", "product lead", "agentic ai", "ai product", "platform & ai")
        if source.type == "imap" and l1_score >= 15 and any(term in title or term in description for term in strong_terms):
            return True
        return False

    def collection_freshness(self) -> Dict:
        last = self.database.last_successful_collection_at()
        if not last:
            return {"queue_last_collected": None, "queue_freshness_minutes": None, "queue_freshness_hours": None, "queue_is_stale": True}
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return {"queue_last_collected": str(last), "queue_freshness_minutes": None, "queue_freshness_hours": None, "queue_is_stale": True}
        age_minutes = max(0, int((datetime.utcnow() - last_dt).total_seconds() // 60))
        return {
            "queue_last_collected": str(last),
            "queue_freshness_minutes": age_minutes,
            "queue_freshness_hours": age_minutes // 60,
            "queue_is_stale": age_minutes >= self.config.collect_stale_minutes,
        }

    def warn_agent_source_collection_failure(self, source: SourceConfig, error: str) -> None:
        if source.created_by != "agent" or source.status not in ("test", "active"):
            return
        log_context(
            LOGGER,
            logging.WARNING,
            "agent_source_collection_failure",
            source_id=source.id,
            url=source.url,
            error=safe_log_text(error, 500),
        )

    def run_l2_relevance(self, rows) -> None:
        for row in rows:
            if self.database.latest_l2_verdict(row["id"]):
                continue
            try:
                verdict = self.llm.relevance(self.profile, row)
            except (BudgetExceeded, LLMError) as exc:
                log_context(LOGGER, logging.WARNING, "l2_relevance_skipped", job_id=row["id"], error=str(exc))
                return
            self.database.save_l2_verdict(
                row["id"],
                verdict["verdict"],
                verdict["priority"],
                verdict["reason"],
                verdict.get("evidence_phrases", []),
                self.config.openai_model if self.config.openai_api_key else "local-fallback",
            )
            log_context(
                LOGGER,
                logging.INFO,
                "l2_relevance_saved",
                job_id=row["id"],
                verdict=verdict["verdict"],
                priority=verdict["priority"],
            )

    def refresh_profile(self) -> None:
        self.profile = load_profile(self.config)
        self.scoring.profile = self.profile

    def action_context(self, confirmed: bool = False) -> AgentActionContext:
        self.refresh_profile()
        return AgentActionContext(
            config=self.config,
            database=self.database,
            profile=self.profile,
            source_reachable=self.source_candidate_reachable,
            shadow_test=self.scoring.shadow_test,
            run_l2=self.run_l2_relevance,
            confirmed=confirmed,
        )

    def apply_action_payload(self, action: Dict, confirmed: bool = False):
        return apply_agent_action(action, self.action_context(confirmed=confirmed))

    def recalculate_source_scores(self) -> None:
        for row in self.database.source_feedback_metrics():
            jobs_seen = int(row["jobs_seen"] or 0)
            irrelevant = int(row["irrelevant_count"] or 0)
            cover_notes = int(row["cover_note_count"] or 0)
            applied = int(row["applied_count"] or 0)
            if jobs_seen == 0:
                continue
            score = 50 + min(25, applied * 12) + min(20, cover_notes * 5) - min(30, irrelevant * 8)
            if jobs_seen >= 10 and irrelevant / float(jobs_seen) > 0.5:
                score -= 15
            self.database.update_source_score(row["id"], max(0, min(100, score)))

    def rescore_recent_jobs(self, limit: int = 500) -> None:
        ruleset = load_scoring_rules(self.config.scoring_path)
        candidates = []
        for row in self.database.recent_jobs(limit):
            job = Job(
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
            result = score_job(job, self.profile, ruleset)
            self.database.save_score(row["id"], result)
            source = SourceConfig(
                id=row["source_id"],
                name=row["source_name"],
                type="imap" if row["source_id"] == "email-job-alerts" else "rss",
                url="imap://job-alerts" if row["source_id"] == "email-job-alerts" else row["url"],
            )
            if self.should_l2_score(source, job, result.score, len(candidates)):
                current = self.database.get_job(row["id"])
                if current:
                    candidates.append(current)
        if candidates:
            self.run_l2_relevance(candidates)
        log_context(LOGGER, logging.INFO, "recent_jobs_rescored", limit=limit, l2_candidates=len(candidates))

    def source_metrics_markdown(self) -> str:
        rows = self.database.source_feedback_metrics()
        lines = ["| source | score | jobs_seen | irrelevant | cover_notes | applied |", "|---|---:|---:|---:|---:|---:|"]
        for row in rows:
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (
                    row["id"],
                    row["current_score"],
                    row["jobs_seen"] or 0,
                    row["irrelevant_count"] or 0,
                    row["cover_note_count"] or 0,
                    row["applied_count"] or 0,
                )
            )
        return "\n".join(lines)

    def append_sources(self, candidates: List[dict]) -> dict:
        path = self.config.sources_path
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        existing_urls = {source.get("url") for source in existing}
        result = {"appended": 0, "skipped_duplicates": 0, "skipped_invalid": 0}
        for candidate in candidates:
            try:
                source_type = normalize_discovered_source_type(candidate.get("type", "json_api"))
            except ValueError as exc:
                result["skipped_invalid"] += 1
                log_context(LOGGER, logging.WARNING, "discovery_candidate_rejected", reason=str(exc))
                continue
            source_url = sanitize_agent_string(candidate.get("url"), 500)
            if source_type == "imap" and not source_url:
                source_url = "imap://job-alerts"
            if not source_url:
                result["skipped_invalid"] += 1
                continue
            if source_url in existing_urls:
                result["skipped_duplicates"] += 1
                continue
            try:
                validate_source_url(source_url, source_type)
                if source_type != "imap":
                    validate_safe_url(source_url)
                    if not self.source_candidate_reachable(source_url):
                        raise SourceError("HEAD probe failed")
            except (ValueError, SourceError) as exc:
                result["skipped_invalid"] += 1
                log_context(LOGGER, logging.WARNING, "discovery_candidate_rejected", url=source_url, reason=str(exc))
                continue
            name = sanitize_agent_string(candidate.get("name") or candidate.get("url") or "agent-source", 80)
            existing.append(
                {
                    "id": slugify(name or source_url or "agent-source"),
                    "name": name or "Agent source",
                    "type": source_type,
                    "url": source_url,
                    "enabled": True,
                    "status": "test",
                    "risk_level": sanitize_agent_string(candidate.get("risk") or candidate.get("risk_level") or "low", 20),
                    "created_by": "agent",
                    "why_it_matches": sanitize_agent_string(candidate.get("why_it_matches", ""), 300),
                    "validation_notes": sanitize_agent_string(candidate.get("validation_notes", ""), 500),
                }
            )
            existing_urls.add(source_url)
            result["appended"] += 1
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.database.upsert_sources(load_sources(path))
        log_context(LOGGER, logging.INFO, "sources_file_updated", path=str(path), **result)
        return result

    def top_ranked_preview(self, limit: int = 5) -> List[Dict]:
        rows = self.database.top_ranked_jobs(limit)
        return [{key: row[key] for key in row.keys()} for row in rows]

    def service_loop(self) -> None:
        from .service import run

        run()

    def touch_heartbeat(self) -> None:
        self.config.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.heartbeat_path.write_text(utc_now_iso(), encoding="utf-8")

    def submit_background(self, fn, *args):
        future = self.executor.submit(fn, *args)
        self.futures.add(future)
        future.add_done_callback(self.futures.discard)
        return future

    def shutdown_executor(self, timeout: int = 30) -> None:
        if not hasattr(self, "executor"):
            return
        self.shutdown_requested = True
        pending = [future for future in getattr(self, "futures", set()) if not future.done()]
        if pending:
            done, not_done = wait(pending, timeout=timeout)
            for future in done:
                exc = future.exception()
                if exc:
                    log_context(LOGGER, logging.ERROR, "background_task_error", error=str(exc))
            for future in not_done:
                future.cancel()
            log_context(LOGGER, logging.INFO, "executor_shutdown_waited", completed=len(done), cancelled=len(not_done))
        self.executor.shutdown(wait=False, cancel_futures=True)

    def handle_shutdown_signal(self, signum, _frame) -> None:
        self.shutdown_requested = True
        log_context(LOGGER, logging.WARNING, "shutdown_signal_received", signum=signum)

    def source_candidate_reachable(self, url: str) -> bool:
        request = urllib.request.Request(url, headers=source_module.DEFAULT_HEADERS, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                validate_safe_url(response.geturl())
                return 200 <= response.status < 400
        except urllib.error.HTTPError as exc:
            if exc.code == 405:
                return self.source_candidate_get_probe(url)
            return 200 <= exc.code < 400
        except SourceError as exc:
            log_context(LOGGER, logging.WARNING, "source_candidate_head_failed", url=url, error=str(exc))
            return False
        except urllib.error.URLError as exc:
            log_context(LOGGER, logging.WARNING, "source_candidate_head_failed", url=url, error=str(exc.reason))
            return False

    def source_candidate_get_probe(self, url: str) -> bool:
        request = urllib.request.Request(url, headers=source_module.DEFAULT_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                validate_safe_url(response.geturl())
                response.read(1)
                return 200 <= response.status < 400
        except SourceError as exc:
            log_context(LOGGER, logging.WARNING, "source_candidate_get_probe_failed", url=url, error=str(exc))
            return False
        except urllib.error.URLError as exc:
            log_context(LOGGER, logging.WARNING, "source_candidate_get_probe_failed", url=url, error=str(exc.reason))
            return False


def run_once() -> None:
    bot = JobHunter.from_environment()
    bot.initialize()
    bot.collect()


def format_usage(usage) -> str:
    return (
        "Usage\n"
        "Jobs today: %(jobs_today)s\n"
        "OpenAI today: $%(today).4f\n"
        "OpenAI month: $%(month).4f\n"
        "Cover notes today: %(cover_notes_today)s\n"
        "Agent actions today: %(agent_actions_today)s"
    ) % usage


def slugify(value: str) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or ""))
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")[:80] or "agent-source"


def normalize_discovered_source_type(value: str) -> str:
    try:
        return normalize_source_type(value or "json_api")
    except ValueError:
        alias = {"api": "json_api", "ats": "greenhouse", "email": "imap"}
        if str(value).lower() in alias:
            return alias[str(value).lower()]
        raise


def sanitize_agent_string(value, max_length: int) -> str:
    text = "".join(char if char >= " " or char in "\n\t" else " " for char in str(value or ""))
    return text.strip()[:max_length]
