import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from .budget import BudgetGate
from .config import AppConfig, ensure_directories, load_app_config, load_profile, load_sources, validate_source_url
from .coordinators import DiscoveryCoordinator, ScoringCoordinator, read_json
from .database import Database, tomorrow_iso
from .llm import BudgetExceeded, LLMClient, LLMError
from .logging_setup import configure_logging, log_context
from .models import SourceConfig, utc_now_iso
from .scoring import load_scoring_rules, score_job
from . import sources as source_module
from .sources import SourceError, collect_from_source
from .telegram import TelegramClient

LOGGER = logging.getLogger(__name__)


class JobBot:
    def __init__(self, config: AppConfig):
        configure_logging()
        self.config = config
        ensure_directories(config)
        self.database = Database(config.database_path)
        self.database.init_schema()
        self.profile = load_profile(config)
        self.telegram = TelegramClient(config.telegram_bot_token, config.telegram_allowed_chat_id)
        self.budget = BudgetGate(config, self.database)
        self.llm = LLMClient(config, self.budget)
        source_module.MAX_BYTES = config.max_response_bytes
        source_module.CHECK_ROBOTS = config.check_robots
        self.discovery = DiscoveryCoordinator(config, self.database, self.profile)
        self.scoring = ScoringCoordinator(config, self.database, self.profile)
        self.executor = ThreadPoolExecutor(max_workers=2)

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
        log_context(LOGGER, logging.INFO, "jobbot_initialized", sources=len(sources), database=str(self.config.database_path))
        print("Initialized jobbot with %s sources and database %s" % (len(sources), self.config.database_path))

    def collect(self) -> None:
        sources = [source for source in load_sources(self.config.sources_path) if source.enabled]
        for source in sources:
            source.imap_last_uid = self.database.source_imap_last_uid(source.id)
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
        try:
            jobs = collect_from_source(source)
            fetched_count = len(jobs)
            for job in jobs:
                job_id, inserted = self.database.upsert_job(job)
                if inserted:
                    inserted_count += 1
                result = score_job(job, self.profile, ruleset)
                self.database.save_score(job_id, result)
            log_context(LOGGER, logging.INFO, "source_collected", source_id=source.id, fetched=fetched_count, inserted=inserted_count)
        except SourceError as exc:
            error = str(exc)
            log_context(LOGGER, logging.WARNING, "source_error", source_id=source.id, error=error)
        except Exception as exc:
            error = "%s: %s" % (exc.__class__.__name__, exc)
            log_context(LOGGER, logging.ERROR, "source_unexpected_error", source_id=source.id, error=error)
        finally:
            self.database.finish_source_run(run_id, source.id, fetched_count, inserted_count, error)
        return fetched_count, inserted_count

    def send_digest(self) -> None:
        rows = self.database.jobs_for_digest(self.config.digest_max_jobs)
        usage = self.database.usage_summary()
        body = (
            "Jobs today: %(jobs_today)s\n"
            "OpenAI today: $%(today).4f\n"
            "OpenAI month: $%(month).4f\n"
            "Cover notes today: %(cover_notes_today)s"
        ) % usage
        self.telegram.send_digest_header("New job matches" if rows else "No strong new matches right now", body)
        sent_ids: List[str] = []
        for row in rows:
            self.telegram.send_job(row)
            sent_ids.append(row["id"])
        digest_id = self.database.mark_digested(sent_ids)
        log_context(LOGGER, logging.INFO, "digest_sent", digest_id=digest_id, job_count=len(sent_ids))

    def poll_telegram_once(self) -> None:
        self.touch_heartbeat()
        self.poll_workspace()
        actions = self.telegram.poll_actions()
        for action in actions:
            self.handle_action(action)

    def handle_action(self, action) -> None:
        log_context(LOGGER, logging.INFO, "telegram_action_received", scope=action.scope, action=action.action, target_id=action.target_id)
        if action.scope == "bot":
            self.handle_bot_action(action)
            return
        if action.scope == "disc":
            self.handle_discovery_action(action)
            return
        if action.scope == "tune":
            self.handle_tuning_action(action)
            return
        if action.scope == "cover":
            self.handle_cover_override(action)
            return
        if action.scope == "job":
            self.handle_job_action(action)
            return
        self.telegram.answer_callback(action.callback_id, "Unknown action")

    def handle_bot_action(self, action) -> None:
        if action.action == "collect":
            allowed, wait = self.database.rate_limit_check("bot:collect", self.config.rate_limit_collect_seconds)
            if not allowed:
                self.telegram.answer_callback(action.callback_id, "Please wait %ss" % wait)
                return
            self.telegram.answer_callback(action.callback_id, "Searching for new jobs...")
            self.executor.submit(self.collect_and_digest)
            return
        if action.action == "discover_sources":
            allowed, _count = self.database.rate_limit_daily("bot:discover_sources", self.config.rate_limit_discovery_per_day)
            if not allowed:
                self.telegram.answer_callback(action.callback_id, "Discovery limit reached today")
                return
            session_id = self.discovery.create_request(load_sources(self.config.sources_path), self.source_metrics_markdown())
            self.database.add_feedback("__system__", "bot:discover_sources", session_id)
            self.telegram.answer_callback(action.callback_id, "Discovery in progress")
            self.telegram.send_message("Discovery request `%s` created. Waiting for OpenClaw response." % session_id)
            return
        if action.action == "tune_scoring":
            allowed, _count = self.database.rate_limit_daily("bot:tune_scoring", self.config.rate_limit_tuning_per_day)
            if not allowed:
                self.telegram.answer_callback(action.callback_id, "Tuning limit reached today")
                return
            session_id = self.scoring.create_request()
            self.database.add_feedback("__system__", "bot:tune_scoring", session_id)
            self.telegram.answer_callback(action.callback_id, "Scoring tuning request created")
            self.telegram.send_message("Scoring tuning request `%s` created. Waiting for OpenClaw response." % session_id)
            return
        if action.action == "usage":
            self.telegram.answer_callback(action.callback_id, "Usage sent")
            self.telegram.send_message(format_usage(self.database.usage_summary()))
            return
        self.telegram.answer_callback(action.callback_id, "Unknown bot action")

    def collect_and_digest(self) -> None:
        self.collect()
        self.send_digest()

    def handle_job_action(self, action) -> None:
        job = self.database.get_job(action.job_id)
        if not job:
            self.telegram.answer_callback(action.callback_id, "Job not found")
            return
        current_status = job["status"]
        if action.action == "irrelevant":
            if current_status == "rejected":
                self.telegram.answer_callback(action.callback_id, "Already marked irrelevant")
                return
            self.database.update_job_status(action.job_id, "rejected")
            self.database.add_feedback(action.job_id, "irrelevant")
            log_context(LOGGER, logging.INFO, "job_marked_irrelevant", job_id=action.job_id, source_id=job["source_id"])
            self.telegram.answer_callback(action.callback_id, "Marked irrelevant")
            self.recalculate_source_scores()
            return
        if action.action == "snooze_1d":
            self.database.update_job_status(action.job_id, "snoozed", snoozed_until=tomorrow_iso())
            self.database.add_feedback(action.job_id, "snooze_1d")
            log_context(LOGGER, logging.INFO, "job_snoozed", job_id=action.job_id, source_id=job["source_id"])
            self.telegram.answer_callback(action.callback_id, "Will remind you tomorrow")
            return
        if action.action == "applied":
            if current_status == "applied":
                self.telegram.answer_callback(action.callback_id, "Already applied")
                return
            self.database.update_job_status(action.job_id, "applied")
            self.database.add_feedback(action.job_id, "applied")
            self.database.promote_source_if_test(job["source_id"])
            log_context(LOGGER, logging.INFO, "job_marked_applied", job_id=action.job_id, source_id=job["source_id"])
            self.telegram.answer_callback(action.callback_id, "Marked applied")
            self.recalculate_source_scores()
            return
        if action.action == "cover_note":
            if not self.rate_limit_cover_note(action):
                return
            self.telegram.answer_callback(action.callback_id, "Generating cover note")
            self.executor.submit(self.generate_cover_note, action.job_id, False)
            return
        self.telegram.answer_callback(action.callback_id, "Unknown job action")

    def rate_limit_cover_note(self, action) -> bool:
        allowed, _count = self.database.rate_limit_daily("cover_note", self.config.rate_limit_cover_notes_per_day)
        if not allowed:
            self.telegram.answer_callback(action.callback_id, "Cover-note limit reached today")
            return False
        return True

    def handle_cover_override(self, action) -> None:
        if action.action == "cancel":
            self.telegram.answer_callback(action.callback_id, "Cancelled")
            return
        if action.action == "override":
            self.telegram.answer_callback(action.callback_id, "Generating with override")
            self.executor.submit(self.generate_cover_note, action.job_id, True)
            return
        self.telegram.answer_callback(action.callback_id, "Unknown cover action")

    def generate_cover_note(self, job_id: str, override_budget: bool = False) -> None:
        job = self.database.get_job(job_id)
        if not job:
            self.telegram.send_message("Job not found for cover note.")
            return
        try:
            draft = self.llm.cover_note(self.profile, job, override_budget=override_budget)
        except BudgetExceeded as exc:
            log_context(LOGGER, logging.WARNING, "cover_note_budget_exceeded", job_id=job_id, reason=exc.reason)
            self.telegram.send_cover_override_prompt(job_id, exc.reason)
            return
        except LLMError as exc:
            log_context(LOGGER, logging.ERROR, "cover_note_llm_error", job_id=job_id, error=str(exc))
            self.telegram.send_message("OpenAI error: %s" % exc)
            return
        self.database.add_feedback(job_id, "cover_note")
        self.database.save_draft(job_id, "cover_note", draft)
        self.database.update_job_status(job_id, "draft_ready")
        self.database.promote_source_if_test(job["source_id"])
        self.telegram.send_message("Cover note for %s - %s:\n\n%s" % (job["title"], job["company"], draft))
        log_context(LOGGER, logging.INFO, "cover_note_ready", job_id=job_id, source_id=job["source_id"], override_budget=override_budget)
        self.recalculate_source_scores()

    def poll_workspace(self) -> None:
        for item in self.discovery.poll_done():
            log_context(LOGGER, logging.INFO, "sending_discovery_approval", session_id=item["session_id"], candidates=len(item["candidates"]))
            self.telegram.send_discovery_approval(item["session_id"], item["candidates"])
        for item in self.scoring.poll_done():
            log_context(LOGGER, logging.INFO, "sending_tuning_approval", session_id=item["session_id"])
            self.telegram.send_tuning_approval(item["session_id"], json.dumps(item["report"], indent=2, sort_keys=True))

    def handle_discovery_action(self, action) -> None:
        row = self.database.get_discovery_run(action.target_id)
        if not row or not row["response_path"]:
            self.telegram.answer_callback(action.callback_id, "Discovery response not ready")
            return
        response = read_json(Path(row["response_path"]))
        candidates = response.get("candidates", [])
        approved = []
        if action.action == "approve":
            if action.index is None:
                approved = candidates
            elif 0 <= action.index < len(candidates):
                approved = [candidates[action.index]]
            self.append_sources(approved)
            self.database.update_discovery_run(action.target_id, status="approved", approved_count=len(approved))
            log_context(LOGGER, logging.INFO, "discovery_sources_approved", session_id=action.target_id, approved=len(approved))
            self.telegram.answer_callback(action.callback_id, "Approved %s source(s)" % len(approved))
        elif action.action == "reject":
            self.database.update_discovery_run(action.target_id, status="rejected")
            log_context(LOGGER, logging.INFO, "discovery_rejected", session_id=action.target_id)
            self.telegram.answer_callback(action.callback_id, "Rejected discovery")

    def append_sources(self, candidates: List[dict]) -> None:
        path = self.config.sources_path
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        existing_urls = {source.get("url") for source in existing}
        appended = 0
        skipped = 0
        for candidate in candidates:
            source_type = normalize_discovered_source_type(candidate.get("type", "json_api"))
            source_url = candidate.get("url")
            if source_type == "imap" and not source_url:
                source_url = "imap://job-alerts"
            if not source_url:
                skipped += 1
                continue
            try:
                validate_source_url(source_url, source_type)
            except ValueError:
                skipped += 1
                continue
            if source_url in existing_urls:
                skipped += 1
                continue
            existing.append(
                {
                    "id": slugify(candidate.get("name") or candidate.get("url") or "agent-source"),
                    "name": candidate.get("name") or "Agent source",
                    "type": source_type,
                    "url": source_url,
                    "enabled": True,
                    "status": "test",
                    "risk_level": candidate.get("risk", "medium"),
                    "created_by": "agent",
                    "validation_notes": candidate.get("validation_notes", ""),
                }
            )
            existing_urls.add(source_url)
            appended += 1
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.database.upsert_sources(load_sources(path))
        log_context(LOGGER, logging.INFO, "sources_file_updated", path=str(path), appended=appended, skipped_duplicates=skipped)

    def handle_tuning_action(self, action) -> None:
        if action.action == "reject":
            self.telegram.answer_callback(action.callback_id, "Rejected scoring proposal")
            return
        if action.action == "diff":
            self.telegram.answer_callback(action.callback_id, "Diff is in workspace")
            self.telegram.send_message("Review tuning files in `%s/tuning`." % self.config.workspace_dir)
            return
        if action.action == "apply":
            proposed_path = self.config.workspace_dir / "tuning" / ("response-%s.json" % action.target_id)
            if not proposed_path.exists():
                self.telegram.answer_callback(action.callback_id, "Scoring response not ready")
                return
            version = self.scoring.apply_rules(action.target_id, proposed_path)
            log_context(LOGGER, logging.INFO, "tuning_applied", session_id=action.target_id, version=version)
            self.telegram.answer_callback(action.callback_id, "Applied scoring v%s" % version)
            self.telegram.send_message("Applied scoring rules version %s." % version)

    def discover_sources(self) -> None:
        session_id = self.discovery.create_request(load_sources(self.config.sources_path), self.source_metrics_markdown())
        print("Discovery request created: %s" % session_id)

    def tune_scoring(self) -> None:
        session_id = self.scoring.create_request()
        print("Tuning request created: %s" % session_id)

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

    def serve(self) -> None:
        self.initialize()
        self.telegram.send_digest_header("Jobbot ready", "Click Get more jobs to search.")
        while True:
            self.poll_telegram_once()
            time.sleep(2)

    def touch_heartbeat(self) -> None:
        self.config.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.heartbeat_path.write_text(utc_now_iso(), encoding="utf-8")


def run_once() -> None:
    bot = JobBot.from_environment()
    bot.initialize()
    bot.collect()
    bot.send_digest()


def format_usage(usage) -> str:
    return (
        "Usage\n"
        "Jobs today: %(jobs_today)s\n"
        "OpenAI today: $%(today).4f\n"
        "OpenAI month: $%(month).4f\n"
        "Cover notes today: %(cover_notes_today)s\n"
        "Last discovery: %(last_discovery)s\n"
        "Last scoring update: %(last_scoring)s"
    ) % usage


def slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48] or "agent-source"


def normalize_discovered_source_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "email_alert":
        return "imap"
    if normalized in ("rss", "json_api", "ats", "community", "imap"):
        return normalized
    return "json_api"
