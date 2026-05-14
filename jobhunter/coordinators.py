import json
from typing import Dict

from .config import AppConfig
from .database import Database
from .models import UserProfile
from .scoring import _json_list, score_job

SUPPORTED_RULE_KINDS = {
    "match_any_word",
    "match_all_word",
    "hard_reject_word",
    "field_equals",
    "numeric_at_least",
    "feedback_similarity",
}


class ScoringCoordinator:
    """Scoring analysis helpers used by approval-gated MCP actions."""

    def __init__(self, config: AppConfig, database: Database, profile: UserProfile):
        self.config = config
        self.database = database
        self.profile = profile

    def shadow_test(self, proposed_rules: Dict) -> Dict:
        recent = self.database.recent_jobs(500)
        proposed_scores = []
        current_scores = []
        false_rejects = 0
        applied_count = 0
        applied_consistent = 0
        rejected_count = 0
        rejected_consistent = 0
        thresholds = proposed_rules.get("thresholds", {}) if isinstance(proposed_rules, dict) else {}
        min_show_score = int(thresholds.get("min_show_score", 50) or 50)
        for row in recent:
            job = row_to_job(row)
            result = score_job(job, self.profile, proposed_rules)
            proposed_scores.append(result.score)
            current_scores.append(int(row["score"] or 0))
            if row["status"] == "applied":
                applied_count += 1
                if result.hard_reject:
                    false_rejects += 1
                if not result.hard_reject and result.score >= min_show_score:
                    applied_consistent += 1
            if row["status"] == "rejected":
                rejected_count += 1
                if result.hard_reject or result.score < min_show_score:
                    rejected_consistent += 1
        current_average = sum(current_scores) / float(len(current_scores) or 1)
        proposed_average = sum(proposed_scores) / float(len(proposed_scores) or 1)
        return {
            "sample_size": len(recent),
            "current_distribution": score_values_distribution(current_scores),
            "proposed_distribution": score_values_distribution(proposed_scores),
            "current_average_score": current_average,
            "proposed_average_score": proposed_average,
            "average_score_shift": proposed_average - current_average,
            "min_score": min(proposed_scores) if proposed_scores else 0,
            "max_score": max(proposed_scores) if proposed_scores else 0,
            "applied_count": applied_count,
            "applied_agreement_rate": applied_consistent / float(applied_count or 1),
            "irrelevant_count": rejected_count,
            "irrelevant_agreement_rate": rejected_consistent / float(rejected_count or 1),
            "false_rejects_applied": false_rejects,
            "false_reject_rate_applied": false_rejects / float(applied_count or 1),
            "training_signals": self.training_signals(),
        }

    def training_signals(self) -> Dict:
        return {
            "applied": [training_signal(row) for row in self.database.feedback_jobs("applied", 50)],
            "irrelevant": [training_signal(row) for row in self.database.feedback_jobs("irrelevant", 50)],
            "cover_note_requested": [training_signal(row) for row in self.database.feedback_jobs("cover_note", 50)],
            "snoozed": [training_signal(row) for row in self.database.feedback_jobs("snooze_1d", 50)],
        }


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


def training_signal(row) -> Dict:
    return {
        "title": row["title"],
        "company": row["company"],
        "source_id": row["source_id"],
        "description_excerpt": (row["description"] or "")[:500],
        "fired_rules": _json_list(row["fired_rules_json"] if "fired_rules_json" in row.keys() else None),
        "score": row["score"] if "score" in row.keys() else None,
        "l2_verdict": row["l2_verdict"] if "l2_verdict" in row.keys() else None,
        "l2_reason": row["l2_reason"] if "l2_reason" in row.keys() else None,
        "feedback_details": row["details"] if "details" in row.keys() else None,
    }


def score_values_distribution(scores) -> Dict:
    buckets = {"0-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    for score in scores:
        if score < 40:
            buckets["0-39"] += 1
        elif score < 60:
            buckets["40-59"] += 1
        elif score < 80:
            buckets["60-79"] += 1
        else:
            buckets["80-100"] += 1
    return buckets


def validate_scoring_ruleset(ruleset: Dict, current_version: int) -> None:
    if not isinstance(ruleset, dict):
        raise ValueError("ruleset must be an object")
    version = ruleset.get("version")
    if not isinstance(version, int):
        raise ValueError("version must be an integer")
    if version < current_version:
        raise ValueError("version must be >= current version")
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    thresholds = ruleset.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object")
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError("rule %s must be an object" % idx)
        if not isinstance(rule.get("id"), str) or not rule.get("id").strip():
            raise ValueError("rule %s must have a string id" % idx)
        kind = rule.get("kind")
        if kind not in SUPPORTED_RULE_KINDS:
            raise ValueError("rule %s has unsupported kind %r" % (rule.get("id") or idx, kind))


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
