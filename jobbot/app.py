import json
import time
from typing import List

from .budget import BudgetGate
from .config import AppConfig, ensure_directories, load_app_config, load_profile, load_sources
from .database import Database, tomorrow_iso
from .llm import LLMClient
from .models import SourceConfig
from .scoring import score_job
from .sources import SourceError, collect_from_source
from .telegram import TelegramClient


class JobBot:
    def __init__(self, config: AppConfig):
        self.config = config
        ensure_directories(config)
        self.database = Database(config.database_path)
        self.database.init_schema()
        self.profile = load_profile(config)
        self.telegram = TelegramClient(config.telegram_bot_token, config.telegram_allowed_chat_id)
        self.budget = BudgetGate(config, self.database)
        self.llm = LLMClient(config, self.budget)

    @classmethod
    def from_environment(cls):
        return cls(load_app_config())

    def initialize(self) -> None:
        sources = load_sources(self.config.sources_path)
        self.database.upsert_sources(sources)
        print("Initialized jobbot with %s sources and database %s" % (len(sources), self.config.database_path))

    def collect(self) -> None:
        sources = [source for source in load_sources(self.config.sources_path) if source.enabled]
        self.database.upsert_sources(sources)
        total_fetched = 0
        total_inserted = 0
        for source in sources:
            fetched, inserted = self.collect_source(source)
            total_fetched += fetched
            total_inserted += inserted
        self.recalculate_source_scores()
        print("Collection complete: fetched=%s inserted=%s" % (total_fetched, total_inserted))

    def collect_source(self, source: SourceConfig):
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
                result = score_job(job, self.profile)
                self.database.save_score(job_id, result)
        except SourceError as exc:
            error = str(exc)
            print("Source error for %s: %s" % (source.id, error))
        except Exception as exc:
            error = "%s: %s" % (exc.__class__.__name__, exc)
            print("Unexpected source error for %s: %s" % (source.id, error))
        finally:
            self.database.finish_source_run(run_id, source.id, fetched_count, inserted_count, error)
        return fetched_count, inserted_count

    def send_digest(self) -> None:
        rows = self.database.jobs_for_digest(self.config.digest_max_jobs)
        if not rows:
            usage = self.database.usage_summary()
            self.telegram.send_message(
                "No strong new matches right now.\nSpend today: $%.4f\nSpend this month: $%.4f"
                % (usage["today"], usage["month"])
            )
            return
        usage = self.database.usage_summary()
        self.telegram.send_message(
            "Top job matches\nJobs: %s\nSpend today: $%.4f\nSpend this month: $%.4f"
            % (len(rows), usage["today"], usage["month"])
        )
        sent_ids: List[str] = []
        for row in rows:
            self.telegram.send_job(row)
            sent_ids.append(row["id"])
        self.database.mark_digested(sent_ids)

    def poll_telegram_once(self) -> None:
        actions = self.telegram.poll_actions()
        for action in actions:
            self.handle_action(action)

    def handle_action(self, action) -> None:
        job = self.database.get_job(action.job_id)
        if not job:
            if action.callback_id:
                self.telegram.answer_callback(action.callback_id, "Job not found")
            return

        if action.action == "irrelevant":
            self.database.update_job_status(action.job_id, "rejected")
            self.database.add_feedback(action.job_id, "irrelevant")
            self.telegram.answer_callback(action.callback_id, "Marked irrelevant")
            self.recalculate_source_scores()
            return

        if action.action == "snooze_1d":
            self.database.update_job_status(action.job_id, "snoozed", snoozed_until=tomorrow_iso())
            self.database.add_feedback(action.job_id, "snooze_1d")
            self.telegram.answer_callback(action.callback_id, "Will remind you tomorrow")
            return

        if action.action == "applied":
            self.database.update_job_status(action.job_id, "applied")
            self.database.add_feedback(action.job_id, "applied")
            self.telegram.answer_callback(action.callback_id, "Marked applied")
            self.recalculate_source_scores()
            return

        if action.action == "cover_note":
            self.database.add_feedback(action.job_id, "cover_note")
            draft = self.llm.cover_note(self.profile, job)
            self.database.save_draft(action.job_id, "cover_note", draft)
            self.database.update_job_status(action.job_id, "draft_ready")
            self.telegram.answer_callback(action.callback_id, "Cover note generated")
            self.telegram.send_message("Cover note for %s - %s:\n\n%s" % (job["title"], job["company"], draft))
            self.recalculate_source_scores()
            return

        self.telegram.answer_callback(action.callback_id, "Unknown action")

    def discover_sources(self) -> None:
        metrics = self.source_metrics_markdown()
        report = self.llm.source_discovery(self.profile, metrics)
        if self.telegram.enabled:
            self.telegram.send_message("Source discovery recommendations:\n\n%s" % report)
        else:
            print(report)

    def recalculate_source_scores(self) -> None:
        for row in self.database.source_feedback_metrics():
            jobs_seen = int(row["jobs_seen"] or 0)
            irrelevant = int(row["irrelevant_count"] or 0)
            cover_notes = int(row["cover_note_count"] or 0)
            applied = int(row["applied_count"] or 0)
            if jobs_seen == 0:
                continue
            score = 50
            score += min(25, applied * 12)
            score += min(20, cover_notes * 5)
            score -= min(30, irrelevant * 8)
            if jobs_seen >= 10 and irrelevant / float(jobs_seen) > 0.5:
                score -= 15
            self.database.update_source_score(row["id"], max(0, min(100, score)))

    def source_metrics_markdown(self) -> str:
        rows = self.database.source_feedback_metrics()
        lines = [
            "| source | score | jobs_seen | irrelevant | cover_notes | applied |",
            "|---|---:|---:|---:|---:|---:|",
        ]
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
        next_collect = 0.0
        while True:
            now = time.time()
            if now >= next_collect:
                self.collect()
                self.send_digest()
                next_collect = now + self.config.collect_interval_minutes * 60
            self.poll_telegram_once()
            time.sleep(2)


def run_once() -> None:
    bot = JobBot.from_environment()
    bot.initialize()
    bot.collect()
    bot.send_digest()
