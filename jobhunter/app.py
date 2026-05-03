import atexit
import difflib
import json
import logging
import re
import signal
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import List

from .budget import BudgetGate
from .agent import AgentCoordinator, read_agent_response
from .agent_actions import AgentActionContext, apply_agent_action
from .config import (
    AppConfig,
    compose_profile,
    ensure_directories,
    ensure_profile_file,
    load_app_config,
    load_profile,
    load_sources,
    split_profile_sections,
    validate_source_url,
)
from .coordinators import DiscoveryCoordinator, ScoringCoordinator, read_json
from .database import Database, tomorrow_iso
from .llm import BudgetExceeded, LLMClient, LLMError
from .logging_setup import configure_logging, log_context, safe_log_text
from .models import Job, SourceConfig, utc_now_iso
from .scoring import load_scoring_rules, score_job
from . import sources as source_module
from .sources import SourceError, VALID_SOURCE_TYPES, collect_from_source, normalize_source_type, validate_safe_url
from .telegram import TelegramClient, TelegramError, revert_keyboard

LOGGER = logging.getLogger(__name__)


class JobHunter:
    def __init__(self, config: AppConfig):
        configure_logging()
        self.config = config
        ensure_directories(config)
        ensure_profile_file(config)
        self.database = Database(config.database_path)
        self.database.init_schema()
        self.profile = load_profile(config)
        self.telegram = TelegramClient(config.telegram_bot_token, config.telegram_allowed_chat_id)
        self.budget = BudgetGate(config, self.database)
        self.llm = LLMClient(config, self.budget)
        source_module.MAX_BYTES = config.max_response_bytes
        source_module.CHECK_ROBOTS = config.check_robots
        source_module.ROBOTS_TXT_RESPECT = config.robots_txt_respect
        self.discovery = DiscoveryCoordinator(config, self.database, self.profile)
        self.scoring = ScoringCoordinator(config, self.database, self.profile)
        self.agent = AgentCoordinator(config, self.database, self.profile)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.futures = set()
        self.agent_apply_in_flight = set()
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
                if inserted and result.score >= 40 and len(l2_candidates) < self.config.l2_max_jobs:
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
        try:
            self.poll_workspace()
            actions = self.telegram.poll_actions()
        except TelegramError as exc:
            log_context(LOGGER, logging.ERROR, "telegram_poll_error", error=str(exc))
            return
        for action in actions:
            try:
                self.handle_action(action)
            except TelegramError as exc:
                log_context(
                    LOGGER,
                    logging.ERROR,
                    "telegram_action_send_error",
                    scope=action.scope,
                    action=action.action,
                    target_id=action.target_id,
                    error=str(exc),
                )

    def handle_action(self, action) -> None:
        log_fields = {"scope": action.scope, "action": action.action, "target_id": action.target_id}
        started_at = time.time()
        text = action_log_text(action)
        if text:
            log_fields["text"] = text
        log_context(
            LOGGER,
            logging.INFO,
            "telegram_action_received",
            **log_fields,
        )
        try:
            if action.scope == "bot":
                self.handle_bot_action(action)
                return
            if action.scope == "agent":
                self.handle_agent_action(action)
                return
            if action.scope == "profile":
                self.handle_profile_action(action)
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
            self.log_action_outcome(action, "refused", "unknown scope", started_at)
            self.answer_action(action, "Unknown action")
        except Exception as exc:
            log_context(
                LOGGER,
                logging.ERROR,
                "telegram_action_exception",
                scope=action.scope,
                action=action.action,
                target_id=action.target_id,
                error=str(exc),
            )
            self.log_action_outcome(action, "failed", exc.__class__.__name__, started_at)
            try:
                self.answer_action(action, "Internal error processing action - see logs")
            except Exception as send_exc:
                log_context(LOGGER, logging.ERROR, "telegram_action_fallback_failed", error=str(send_exc))

    def handle_bot_action(self, action) -> None:
        if action.action == "collect":
            allowed, wait = self.database.rate_limit_check("bot:collect", self.config.rate_limit_collect_seconds)
            if not allowed:
                self.answer_action(action, "Please wait %ss" % wait)
                self.log_action_outcome(action, "rate_limited", "wait_%ss" % wait)
                return
            self.answer_action(action, "Showing latest indexed jobs...")
            self.send_digest()
            stale, age_minutes = self.collection_is_stale()
            if stale:
                self.telegram.send_message("Refreshing sources in background for next time.")
                self.submit_background(self.collect)
                self.log_action_outcome(action, "queued", "background_refresh")
            else:
                self.telegram.send_message("Sources checked %sm ago; skipping refresh." % age_minutes)
                self.log_action_outcome(action, "served", "cache_fresh")
            return
        if action.action == "refresh_collect":
            allowed, wait = self.database.rate_limit_check("bot:collect", self.config.rate_limit_collect_seconds)
            if not allowed:
                self.answer_action(action, "Please wait %ss" % wait)
                self.log_action_outcome(action, "rate_limited", "wait_%ss" % wait)
                return
            self.answer_action(action, "Refreshing sources now...")
            self.submit_background(self.collect)
            self.log_action_outcome(action, "queued", "forced_refresh")
            return
        if action.action == "discover_sources":
            allowed, _count = self.database.rate_limit_daily("bot:discover_sources", self.config.rate_limit_discovery_per_day)
            if not allowed:
                self.answer_action(action, "Discovery limit reached today")
                self.log_action_outcome(action, "rate_limited", "daily_discovery")
                return
            self.start_agent_request("Please run a source discovery strategy cycle and propose new high-signal sources.", action)
            self.log_action_outcome(action, "queued", "agent_discovery")
            return
        if action.action == "tune_scoring":
            allowed, _count = self.database.rate_limit_daily("bot:tune_scoring", self.config.rate_limit_tuning_per_day)
            if not allowed:
                self.answer_action(action, "Tuning limit reached today")
                self.log_action_outcome(action, "rate_limited", "daily_tuning")
                return
            self.start_agent_request("Please propose scoring/filtering improvements based on recent feedback and job quality.", action)
            self.log_action_outcome(action, "queued", "agent_tuning")
            return
        if action.action == "usage":
            self.answer_action(action, "Usage sent", send_message_if_no_callback=False)
            self.telegram.send_message(format_usage(self.database.usage_summary()))
            self.log_action_outcome(action, "served", "usage")
            return
        if action.action == "agent":
            self.start_agent_request(action.text, action)
            return
        if action.action == "agent_help":
            self.answer_action(action, "Use /agent <request>, or just type your request as a normal message.")
            return
        if action.action == "history":
            self.answer_action(action, "History sent", send_message_if_no_callback=False)
            self.telegram.send_message(self.format_agent_history())
            return
        if action.action == "revert":
            self.handle_revert_action(action)
            return
        if action.action == "confirm":
            self.handle_confirm_action(action)
            return
        if action.action in ("list_applied", "list_snoozed", "list_irrelevant"):
            status = {"list_applied": "applied", "list_snoozed": "snoozed", "list_irrelevant": "rejected"}[action.action]
            self.answer_action(action, "List sent", send_message_if_no_callback=False)
            self.telegram.send_message(self.format_jobs_by_status(status))
            return
        if action.action == "scoring_history":
            self.answer_action(action, "Scoring history sent", send_message_if_no_callback=False)
            self.telegram.send_message(self.format_scoring_history())
            return
        if action.action == "menu":
            self.telegram.send_digest_header("Jobhunter ready", "Use the keyboard buttons below.")
            self.log_action_outcome(action, "served", "menu")
            return
        self.log_action_outcome(action, "refused", "unknown bot action")
        self.answer_action(action, "Unknown bot action")

    def start_agent_request(self, text: str, action) -> None:
        active = self.database.active_agent_run()
        if active:
            self.answer_action(action, "Agent already processing", send_message_if_no_callback=False)
            self.telegram.send_message(format_pending_agent_message(active))
            return
        allowed, wait = self.database.rate_limit_check("bot:agent", self.config.rate_limit_agent_seconds)
        if not allowed:
            self.answer_action(action, "Please wait %ss before another /agent request" % wait)
            return
        allowed, count = self.database.rate_limit_daily("bot:agent", self.config.rate_limit_agent_per_day)
        if not allowed:
            self.answer_action(action, "Daily agent quota reached (%s/%s)" % (count, self.config.rate_limit_agent_per_day))
            return
        self.refresh_profile()
        session_id = self.agent.create_request(text)
        self.database.add_feedback("__system__", "bot:agent", session_id)
        self.answer_action(action, "Processing request", send_message_if_no_callback=False)
        sent = self.telegram.send_message(
            "Processing your request: '%s'...\nSession: %s\nAudit: daily quota %s/%s"
            % (safe_log_text(text, 80), session_id, count + 1, self.config.rate_limit_agent_per_day)
        )
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if message_id:
            self.database.update_agent_run(session_id, placeholder_message_id=message_id)

    def answer_action(self, action, text: str, send_message_if_no_callback: bool = True) -> None:
        try:
            if action.callback_id:
                self.telegram.answer_callback(action.callback_id, text)
            elif send_message_if_no_callback:
                self.telegram.send_message(text)
        except TelegramError as exc:
            log_context(
                LOGGER,
                logging.ERROR,
                "answer_action_failed",
                scope=action.scope,
                action=action.action,
                target_id=action.target_id,
                callback=bool(action.callback_id),
                error=str(exc),
            )
            raise

    def log_action_outcome(self, action, outcome: str, reason: str = "", started_at: float = None) -> None:
        elapsed_ms = int((time.time() - started_at) * 1000) if started_at else 0
        log_context(
            LOGGER,
            logging.INFO,
            "action_outcome",
            scope=action.scope,
            action=action.action,
            target_id=action.target_id,
            outcome=outcome,
            reason=reason,
            elapsed_ms=elapsed_ms,
        )

    def collection_is_stale(self):
        last = self.database.last_successful_collection_at()
        if not last:
            return True, "never"
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return True, "unknown"
        age_minutes = max(0, int((datetime.utcnow() - last_dt).total_seconds() // 60))
        return age_minutes >= self.config.collect_stale_minutes, age_minutes

    def warn_agent_source_collection_failure(self, source: SourceConfig, error: str) -> None:
        if source.created_by != "agent" or source.status not in ("test", "active"):
            return
        try:
            self.telegram.send_message(
                "Agent-added source failed its collection test: %s (%s)\nReason: %s"
                % (source.id, source.url, safe_log_text(error, 500))
            )
        except TelegramError as exc:
            log_context(LOGGER, logging.WARNING, "agent_source_failure_warning_failed", source_id=source.id, error=str(exc))

    def edit_or_send_status(self, message_id, text: str) -> None:
        if message_id and hasattr(self.telegram, "edit_message_text"):
            try:
                if len(text) <= 4000:
                    self.telegram.edit_message_text(message_id, text)
                    return
            except TelegramError as exc:
                log_context(LOGGER, logging.WARNING, "telegram_status_edit_failed", message_id=message_id, error=str(exc))
                if hasattr(self.telegram, "delete_message"):
                    self.telegram.delete_message(message_id)
        if hasattr(self.telegram, "send_long_message"):
            self.telegram.send_long_message(text)
        else:
            self.telegram.send_message(text)

    def collect_and_digest(self) -> None:
        try:
            self.collect()
            self.send_digest()
        except TelegramError as exc:
            log_context(LOGGER, logging.ERROR, "telegram_error_in_background_digest", error=str(exc))

    def send_codex_request_notice(self, label: str, session_id: str, manual_handoff: str) -> None:
        if self.config.codex_handoff_mode == "manual":
            self.telegram.send_long_message(manual_handoff)
            return
        handoff_path = self.config.workspace_dir / ("discovery" if label == "source discovery" else "tuning") / ("handoff-%s.md" % session_id)
        self.telegram.send_message(
            "%s queued for automated OpenClaw/Codex worker.\nSession: %s\nFallback prompt: %s"
            % (label.capitalize(), session_id, handoff_path)
        )
        log_context(LOGGER, logging.INFO, "codex_request_queued", label=label, session_id=session_id, handoff_path=str(handoff_path))

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
            if hasattr(self.telegram, "delete_message"):
                self.telegram.delete_message(action.message_id)
            self.telegram.send_message("Why was it irrelevant? Reply with a one-line pattern if there is something I should learn.")
            self.recalculate_source_scores()
            return
        if action.action == "snooze_1d":
            self.database.update_job_status(action.job_id, "snoozed", snoozed_until=tomorrow_iso())
            self.database.add_feedback(action.job_id, "snooze_1d")
            log_context(LOGGER, logging.INFO, "job_snoozed", job_id=action.job_id, source_id=job["source_id"])
            self.telegram.answer_callback(action.callback_id, "Will remind you tomorrow")
            if hasattr(self.telegram, "delete_message"):
                self.telegram.delete_message(action.message_id)
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
            if hasattr(self.telegram, "delete_message"):
                self.telegram.delete_message(action.message_id)
            self.recalculate_source_scores()
            return
        if action.action == "cover_note":
            if current_status in ("applied", "rejected"):
                self.telegram.answer_callback(
                    action.callback_id,
                    "Already applied - cover note skipped" if current_status == "applied" else "Already irrelevant",
                )
                log_context(LOGGER, logging.INFO, "cover_note_skipped_terminal_status", job_id=action.job_id, status=current_status)
                return
            if not self.rate_limit_cover_note(action):
                return
            self.telegram.answer_callback(action.callback_id, "Generating cover note")
            self.submit_background(self.generate_cover_note, action.job_id, False)
            if hasattr(self.telegram, "delete_message"):
                self.telegram.delete_message(action.message_id)
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
            self.submit_background(self.generate_cover_note, action.job_id, True)
            return
        self.telegram.answer_callback(action.callback_id, "Unknown cover action")

    def generate_cover_note(self, job_id: str, override_budget: bool = False) -> None:
        job = self.database.get_job(job_id)
        if not job:
            self.telegram.send_message("Job not found for cover note.")
            return
        if job["status"] in ("applied", "rejected"):
            self.telegram.send_message(
                "Cover note skipped for %s - status is already %s." % (job["title"], job["status"])
            )
            log_context(LOGGER, logging.INFO, "cover_note_worker_skipped_terminal_status", job_id=job_id, status=job["status"])
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
            if item.get("error"):
                log_context(LOGGER, logging.WARNING, "sending_tuning_failure", session_id=item["session_id"], error=item["error"])
                self.telegram.send_message("Scoring tuning failed for session %s: %s" % (item["session_id"], item["error"]))
                continue
            log_context(LOGGER, logging.INFO, "sending_tuning_approval", session_id=item["session_id"])
            self.telegram.send_tuning_approval(item["session_id"], json.dumps(item["report"], indent=2, sort_keys=True))
        for item in self.agent.poll_done():
            run = self.database.get_agent_run(item["session_id"])
            placeholder_message_id = run["placeholder_message_id"] if run and "placeholder_message_id" in run.keys() else None
            if item.get("error"):
                log_context(LOGGER, logging.WARNING, "sending_agent_failure", session_id=item["session_id"], error=item["error"])
                self.edit_or_send_status(placeholder_message_id, friendly_agent_error(item["error"]))
                continue
            log_context(LOGGER, logging.INFO, "sending_agent_response", session_id=item["session_id"])
            self.telegram.send_agent_response(item["session_id"], item["response"], message_id=placeholder_message_id)

    def handle_agent_action(self, action) -> None:
        if action.action == "reject":
            self.telegram.answer_callback(action.callback_id, "Rejected agent actions")
            return
        if action.action != "apply":
            self.telegram.answer_callback(action.callback_id, "Unknown agent action")
            return
        response = read_agent_response(self.config, action.target_id)
        actions = response.get("proposed_actions") or []
        selected = [item for item in actions if item.get("kind") != "data_answer"] if action.index is None else [actions[action.index]] if 0 <= action.index < len(actions) else []
        if selected and selected[0].get("kind") == "data_answer":
            self.telegram.answer_callback(action.callback_id, "Read-only answer already shown")
            return
        if not selected:
            self.telegram.answer_callback(action.callback_id, "No action selected")
            return
        existing_messages = []
        pending_selected = []
        for proposed in selected:
            existing = self.database.find_applied_agent_action(
                action.target_id,
                proposed.get("kind", ""),
                proposed.get("payload", {}),
            )
            if existing:
                if existing["status"] == "pending_confirm":
                    existing_messages.append(
                        "#%s %s: already pending confirmation. Reply `CONFIRM %s` to apply."
                        % (existing["id"], existing["kind"], existing["id"])
                    )
                else:
                    existing_messages.append("#%s %s: already applied, skipped duplicate click" % (existing["id"], existing["kind"]))
            else:
                pending_selected.append(proposed)
        if not pending_selected:
            ack = "Already applied" if existing_messages else "No action selected"
            self.telegram.answer_callback(action.callback_id, ack)
            self.edit_or_send_status(
                action.message_id,
                "Agent action results:\n%s\n\nAudit: session %s" % ("\n".join(existing_messages), action.target_id),
            )
            return
        if action.target_id in self.agent_apply_in_flight:
            self.telegram.answer_callback(action.callback_id, "Still applying - please wait.")
            return
        self.agent_apply_in_flight.add(action.target_id)
        if action.callback_id:
            self.telegram.answer_callback(action.callback_id, "Applying agent action(s)...")
        self.edit_or_send_status(
            action.message_id,
            "Applying - %s...\nAudit: session %s" % (safe_log_text(agent_apply_summary(pending_selected), 120), action.target_id),
        )
        context = AgentActionContext(
            config=self.config,
            database=self.database,
            profile=self.profile,
            source_reachable=self.source_candidate_reachable,
            shadow_test=self.scoring.shadow_test,
            run_l2=self.run_l2_relevance,
        )
        applied = 0
        messages = list(existing_messages)
        run = self.database.get_agent_run(action.target_id)
        user_intent = run["user_text"] if run else response.get("user_intent_summary", "")
        try:
            for proposed in pending_selected:
                result = apply_agent_action(proposed, context)
                status = "pending_confirm" if result.requires_confirm else "applied" if result.applied else "failed"
                row_id = self.database.record_agent_action(
                    action.target_id,
                    proposed.get("kind", ""),
                    user_intent,
                    proposed.get("summary", ""),
                    proposed.get("payload", {}),
                    status,
                    archive_path=result.archive_path,
                    target_path=result.target_path,
                    result_message=result.message,
                )
                if result.applied:
                    applied += 1
                    if proposed.get("kind") in ("scoring_rule_proposal", "profile_edit"):
                        self.rescore_recent_jobs()
                        self.send_ranking_preview("Preview after %s" % proposed.get("kind"), row_id)
                if result.requires_confirm:
                    messages.append("#%s %s: %s. Reply `CONFIRM %s` to apply." % (row_id, proposed.get("kind", ""), result.message, row_id))
                else:
                    messages.append("#%s %s: %s" % (row_id, proposed.get("kind", ""), result.message))
                log_context(
                    LOGGER,
                    logging.INFO if result.applied or result.requires_confirm else logging.WARNING,
                    "agent_action_pending_confirm" if result.requires_confirm else "agent_action_applied" if result.applied else "agent_action_failed",
                    action_id=row_id,
                    kind=proposed.get("kind"),
                    result_message=result.message,
                )
        finally:
            self.agent_apply_in_flight.discard(action.target_id)
        pending = len([line for line in messages if "Reply `CONFIRM " in line])
        heading = "Applied" if applied else "Agent action results"
        if pending:
            heading = "Confirmation needed"
        self.edit_or_send_status(action.message_id, "%s - %s\n\nAudit: session %s" % (heading, "\n".join(messages), action.target_id))

    def handle_confirm_action(self, action) -> None:
        try:
            action_id = int(str(action.target_id or "").strip())
        except ValueError:
            self.answer_action(action, "Use CONFIRM <agent_action_id>")
            return
        row = self.database.get_agent_action(action_id)
        if not row:
            self.answer_action(action, "No agent action #%s" % action_id)
            return
        if row["status"] != "pending_confirm":
            self.answer_action(action, "Agent action #%s is not pending confirmation" % action_id)
            return
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            self.answer_action(action, "Agent action #%s has invalid payload" % action_id)
            return
        context = AgentActionContext(
            config=self.config,
            database=self.database,
            profile=self.profile,
            source_reachable=self.source_candidate_reachable,
            shadow_test=self.scoring.shadow_test,
            run_l2=self.run_l2_relevance,
            confirmed=True,
        )
        result = apply_agent_action({"kind": row["kind"], "payload": payload}, context)
        status = "applied" if result.applied else "failed"
        self.database.update_agent_action_result(
            action_id,
            status,
            archive_path=result.archive_path,
            target_path=result.target_path,
            result_message=result.message,
        )
        log_context(
            LOGGER,
            logging.INFO if result.applied else logging.WARNING,
            "agent_action_confirmed" if result.applied else "agent_action_confirm_failed",
            action_id=action_id,
            kind=row["kind"],
            result_message=result.message,
        )
        self.answer_action(action, "Confirmed action #%s" % action_id, send_message_if_no_callback=False)
        self.telegram.send_message("Confirmed agent action #%s: %s\nAudit: status %s" % (action_id, result.message, status))
        if result.applied and row["kind"] in ("scoring_rule_proposal", "profile_edit"):
            self.rescore_recent_jobs()
            self.send_ranking_preview("Preview after confirmed %s" % row["kind"], action_id)

    def handle_revert_action(self, action) -> None:
        try:
            action_id = int(str(action.target_id or "").strip())
        except ValueError:
            self.answer_action(action, "Use /revert <action_id>")
            return
        row = self.database.get_agent_action(action_id)
        if not row:
            self.answer_action(action, "Agent action #%s not found" % action_id)
            return
        if row["status"] == "reverted":
            self.answer_action(action, "Agent action #%s already reverted" % action_id)
            return
        archive_path = Path(row["archive_path"] or "")
        target_path = Path(row["target_path"] or "")
        if not archive_path.exists() or not str(target_path):
            self.answer_action(action, "Agent action #%s has no reversible archive" % action_id)
            return
        shutil.copyfile(archive_path, target_path)
        self.database.update_agent_action_status(action_id, "reverted")
        revert_id = self.database.record_agent_action(
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
        self.answer_action(action, "Reverted action #%s as audit #%s" % (action_id, revert_id))

    def handle_profile_action(self, action) -> None:
        self.refresh_profile()
        sections = split_profile_sections(self.profile.raw_text)
        if action.action == "show":
            self.telegram.send_message(
                "# About me\n%s\n\nDirectives: %s line(s)" % (sections["about_me"] or "(empty)", len([line for line in sections["directives"].splitlines() if line.strip()]))
            )
            return
        if action.action == "set":
            new_about = action.text.strip()
            if not new_about:
                self.telegram.send_message("Use /profile set <new About me text>")
                return
            archive = self.config.profile_path.with_name("profile.%s.md.bak" % utc_now_iso().replace(":", "").replace("-", ""))
            shutil.copyfile(self.config.profile_path, archive)
            old_lines = sections["about_me"].splitlines()
            new_lines = new_about.splitlines()
            diff = "\n".join(difflib.unified_diff(old_lines, new_lines, fromfile="old About me", tofile="new About me", lineterm=""))
            self.config.profile_path.write_text(compose_profile(new_about, sections["directives"]), encoding="utf-8")
            self.refresh_profile()
            self.telegram.send_message("Profile About me replaced.\nArchive: %s\n\n%s" % (archive, diff[:3000]))
            return
        if action.action == "refine":
            self.start_agent_request("Refine the # About me wording for clarity and grammar without changing intent.", action)
            return
        self.telegram.send_message("Unknown profile action")

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
        self.discovery.profile = self.profile
        self.scoring.profile = self.profile
        self.agent.profile = self.profile

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
            result = self.append_sources(approved)
            self.database.update_discovery_run(action.target_id, status="approved", approved_count=result["appended"])
            log_context(
                LOGGER,
                logging.INFO,
                "discovery_sources_approved",
                session_id=action.target_id,
                approved=result["appended"],
                skipped_duplicates=result["skipped_duplicates"],
                skipped_invalid=result["skipped_invalid"],
            )
            skipped = result["skipped_duplicates"] + result["skipped_invalid"]
            self.telegram.answer_callback(action.callback_id, "Approved %s, skipped %s" % (result["appended"], skipped))
        elif action.action == "reject":
            self.database.update_discovery_run(action.target_id, status="rejected")
            log_context(LOGGER, logging.INFO, "discovery_rejected", session_id=action.target_id)
            self.telegram.answer_callback(action.callback_id, "Rejected discovery")

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
            except ValueError as exc:
                result["skipped_invalid"] += 1
                log_context(LOGGER, logging.WARNING, "discovery_candidate_rejected", url=source_url, reason=str(exc))
                continue
            except SourceError as exc:
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
            try:
                version = self.scoring.apply_rules(action.target_id, proposed_path)
            except ValueError as exc:
                log_context(LOGGER, logging.WARNING, "invalid_scoring_rules_rejected", session_id=action.target_id, error=str(exc))
                self.telegram.answer_callback(action.callback_id, "Invalid ruleset, scoring unchanged: %s" % exc)
                return
            log_context(LOGGER, logging.INFO, "tuning_applied", session_id=action.target_id, version=version)
            self.telegram.answer_callback(action.callback_id, "Applied scoring v%s" % version)
            self.telegram.send_message("Applied scoring rules version %s." % version)
            self.rescore_recent_jobs()
            self.send_ranking_preview("Preview after scoring v%s" % version)

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
            if result.score >= 40 and len(candidates) < self.config.l2_max_jobs:
                current = self.database.get_job(row["id"])
                if current:
                    candidates.append(current)
        if candidates:
            self.run_l2_relevance(candidates)
        log_context(LOGGER, logging.INFO, "recent_jobs_rescored", limit=limit, l2_candidates=len(candidates))

    def send_ranking_preview(self, title: str, action_id=None) -> None:
        rows = self.database.top_ranked_jobs(5)
        if not rows:
            self.telegram.send_message("%s\nNo ranked jobs are indexed yet." % title)
            return
        lines = [title, "Top 5 indexed jobs under current profile/scoring:"]
        for idx, row in enumerate(rows, start=1):
            reason = row["l2_reason"] if "l2_reason" in row.keys() and row["l2_reason"] else "no L2 reason yet"
            lines.append(
                "%s. %s - %s | total %s (L1 %s + L2 %s) | %s"
                % (
                    idx,
                    row["title"],
                    row["company"],
                    row["total_score"] or row["score"] or 0,
                    row["l1_score"] or 0,
                    row["l2_score"] or 0,
                    safe_log_text(reason, 160),
                )
            )
        reply_markup = None
        if action_id:
            lines.append("Revert with /revert %s" % action_id)
            reply_markup = revert_keyboard(action_id)
        self.telegram.send_message("\n".join(lines), reply_markup=reply_markup)

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

    def format_agent_history(self) -> str:
        rows = self.database.recent_agent_actions(10)
        if not rows:
            return "No agent actions yet."
        lines = ["Recent agent actions:"]
        for row in rows:
            lines.append(
                "#%s %s %s - %s"
                % (row["id"], row["kind"], row["status"], (row["summary"] or row["result_message"] or "")[:120])
            )
        lines.append("\nUse /revert <id> for reversible applied file edits.")
        return "\n".join(lines)

    def format_jobs_by_status(self, status: str) -> str:
        rows = self.database.recent_jobs_by_status(status, 10)
        if not rows:
            return "No recent %s jobs." % status
        lines = ["Recent %s jobs:" % status]
        for row in rows:
            lines.append("- %s - %s (%s)\n  %s" % (row["title"], row["company"], row["score"] or 0, row["url"]))
        return "\n".join(lines)

    def format_scoring_history(self) -> str:
        with self.database.connection() as conn:
            rows = list(conn.execute("select * from scoring_versions order by id desc limit 10"))
        if not rows:
            return "No scoring versions recorded yet."
        lines = ["Scoring history:"]
        for row in rows:
            lines.append("v%s %s %s" % (row["version"], row["status"], row["activated_at"] or ""))
        return "\n".join(lines)

    def serve(self) -> None:
        self.initialize()
        try:
            self.telegram.send_digest_header("Jobhunter ready", "Use the keyboard buttons below.")
        except TelegramError as exc:
            log_context(LOGGER, logging.ERROR, "telegram_ready_message_failed", error=str(exc))
        try:
            while not self.shutdown_requested:
                self.poll_telegram_once()
                time.sleep(2)
        finally:
            self.shutdown_executor()

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
    bot.send_digest()


def friendly_agent_error(error: str) -> str:
    text = str(error or "").lower()
    if "agent_no_tools_used" in text:
        return (
            "Agent didn't inspect any data before answering — refusing to risk a hallucinated reply. "
            "Try rephrasing more specifically (mention a file, job URL, or 'why')."
        )
    if "cap exceeded" in text:
        return "Agent hit a per-request cap (turns / queries / wall time). Try a narrower question."
    if "prompt too large" in text:
        return "Request too large after expansion. Ask one question at a time or be more specific."
    return "Agent request failed: %s" % error


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


def format_pending_agent_message(row) -> str:
    user_text = safe_log_text(row["user_text"], 80)
    age = pending_age_seconds(row["requested_at"])
    return (
        "Still processing your previous request: '%s' (started %ss ago).\n"
        "You can still use Get more jobs, Apply on existing proposals, Usage, or per-job buttons. "
        "Please wait before sending another /agent request."
    ) % (user_text, age)


def pending_age_seconds(requested_at: str) -> int:
    try:
        started = datetime.fromisoformat(str(requested_at).replace("Z", "+00:00"))
        now = datetime.utcnow().replace(tzinfo=started.tzinfo)
        return max(0, int((now - started).total_seconds()))
    except ValueError:
        return 0


def action_log_text(action) -> str:
    if getattr(action, "text", ""):
        return safe_log_text(action.text, 80)
    if action.scope == "bot":
        return {
            "collect": "Get more jobs",
            "discover_sources": "Update sources",
            "tune_scoring": "Tune scoring",
            "usage": "Usage",
            "menu": "Menu",
            "history": "/history",
            "revert": "/revert",
        }.get(action.action, "")
    if action.scope == "agent":
        if action.action == "apply":
            return "Apply all" if action.index is None else "Apply %s" % (action.index + 1)
        if action.action == "reject":
            return "Reject all"
    if action.scope == "job":
        return {
            "irrelevant": "Irrelevant",
            "snooze_1d": "Remind me tomorrow",
            "cover_note": "Give me cover note",
            "applied": "Applied",
        }.get(action.action, "")
    return ""


def agent_apply_summary(actions) -> str:
    summaries = [str(action.get("summary") or action.get("kind") or "action") for action in actions]
    if len(summaries) == 1:
        return summaries[0]
    return "%s actions" % len(summaries)


def slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48] or "agent-source"


def normalize_discovered_source_type(value: str) -> str:
    normalized = normalize_source_type(value or "json_api")
    if normalized not in VALID_SOURCE_TYPES:
        raise ValueError("invalid source type '%s'; allowed: %s" % (value, "/".join(sorted(VALID_SOURCE_TYPES))))
    return normalized


def sanitize_agent_string(value, max_length: int) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = "".join(char if char >= " " and char != "\x7f" else " " for char in text)
    text = " ".join(text.split())
    return text[:max_length]
