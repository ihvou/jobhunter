import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Job, ScoreResult, UserProfile


DEFAULT_RULES = {
    "version": 1,
    "generated_by": "baseline",
    "rules": [],
    "thresholds": {"min_show_score": 50, "hard_reject_floor": 0},
}


def load_scoring_rules(path: Path) -> Dict:
    if not path.exists():
        return DEFAULT_RULES
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def score_job(job: Job, profile: UserProfile, ruleset: Optional[Dict] = None) -> ScoreResult:
    ruleset = ruleset or DEFAULT_RULES
    total = 0
    reasons: List[str] = []
    concerns: List[str] = []
    breakdown: Dict[str, int] = {}
    fired_rules: List[str] = []
    hard_reject = False

    for rule in ruleset.get("rules", []):
        result = evaluate_rule(rule, job, profile)
        rule_id = str(rule.get("id", "unnamed_rule"))
        if result.hard_reject:
            hard_reject = True
            concerns.append("%s: matched %s" % (rule_id, result.matched_label or "hard reject"))
            fired_rules.append(rule_id)
        if result.score:
            total += result.score
            breakdown[rule_id] = result.score
            reasons.append("%s: %s" % (rule_id, result.matched_label or "matched"))
            fired_rules.append(rule_id)

    if not ruleset.get("rules"):
        return fallback_score(job, profile)

    if hard_reject:
        total = int(ruleset.get("thresholds", {}).get("hard_reject_floor", 0))
    total = max(0, min(100, total))
    if not reasons and not hard_reject:
        concerns.append("No positive scoring rules matched")
    return ScoreResult(
        score=total,
        hard_reject=hard_reject,
        reasons=dedupe(reasons)[:6],
        concerns=dedupe(concerns)[:5],
        breakdown=breakdown,
        fired_rules=dedupe(fired_rules),
    )


class RuleResult:
    def __init__(self, score: int = 0, hard_reject: bool = False, matched_label: str = ""):
        self.score = score
        self.hard_reject = hard_reject
        self.matched_label = matched_label


def evaluate_rule(rule: Dict, job: Job, profile: UserProfile) -> RuleResult:
    kind = rule.get("kind")
    if kind == "match_any_word":
        return match_any_word(rule, job)
    if kind == "match_all_word":
        return match_all_word(rule, job)
    if kind == "hard_reject_word":
        result = match_any_word(rule, job)
        return RuleResult(hard_reject=bool(result.matched_label), matched_label=result.matched_label)
    if kind == "field_equals":
        field = str(rule.get("field", ""))
        actual = normalize(get_job_field(job, field))
        expected = normalize(rule.get("value", ""))
        if actual == expected:
            return RuleResult(score=int(rule.get("weight", 0)), matched_label="%s == %s" % (field, expected))
        return RuleResult()
    if kind == "numeric_at_least":
        field = str(rule.get("field", ""))
        actual = get_numeric_field(job, field)
        threshold = int(rule.get("threshold", 0))
        hard_reject_below = bool(rule.get("hard_reject_below", False))
        if actual is None:
            return RuleResult()
        if actual >= threshold:
            return RuleResult(score=int(rule.get("weight", 0)), matched_label="%s >= %s" % (field, threshold))
        if hard_reject_below:
            return RuleResult(hard_reject=True, matched_label="%s below %s" % (field, threshold))
        return RuleResult()
    if kind == "feedback_similarity":
        return feedback_similarity(rule, job, profile)
    return RuleResult()


def match_any_word(rule: Dict, job: Job) -> RuleResult:
    text_by_field = joined_fields(job, rule.get("fields", []))
    for pattern in rule.get("patterns", []):
        matched_field = word_boundary_search(str(pattern), text_by_field)
        if matched_field:
            return RuleResult(score=int(rule.get("weight", 0)), matched_label='matched "%s"' % pattern)
    return RuleResult()


def match_all_word(rule: Dict, job: Job) -> RuleResult:
    text_by_field = joined_fields(job, rule.get("fields", []))
    missing = []
    matched = []
    for pattern in rule.get("patterns", []):
        if word_boundary_search(str(pattern), text_by_field):
            matched.append(str(pattern))
        else:
            missing.append(str(pattern))
    if not missing and matched:
        return RuleResult(score=int(rule.get("weight", 0)), matched_label="matched all: %s" % ", ".join(matched))
    return RuleResult()


def feedback_similarity(rule: Dict, job: Job, profile: UserProfile) -> RuleResult:
    patterns = rule.get("patterns") or profile.positive_keywords
    if not patterns:
        return RuleResult()
    text = joined_fields(job, rule.get("fields", ["title", "description", "company"]))
    hits = [pattern for pattern in patterns if word_boundary_search(str(pattern), text)]
    if not hits:
        return RuleResult()
    fraction = min(1.0, len(hits) / max(1.0, float(rule.get("min_hits", 3))))
    score = int(int(rule.get("weight", 0)) * fraction)
    return RuleResult(score=score, matched_label="similar tokens: %s" % ", ".join(hits[:4]))


def joined_fields(job: Job, fields: Iterable[str]) -> str:
    values = [get_job_field(job, field) for field in fields]
    return "\n".join(str(value or "") for value in values)


def get_job_field(job: Job, field: str):
    return getattr(job, field, "")


def get_numeric_field(job: Job, field: str) -> Optional[int]:
    value = getattr(job, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def word_boundary_search(pattern: str, text: str) -> bool:
    pattern = normalize(pattern)
    if not pattern:
        return False
    tokens = re.findall(r"[a-z0-9+#.-]+", pattern)
    if not tokens:
        return False
    token_pattern = r"(?<![a-z0-9-])" + r"[\s\-/_.]+".join(re.escape(token) for token in tokens) + r"(?![a-z0-9-])"
    return re.search(token_pattern, normalize(text), re.IGNORECASE) is not None


def normalize(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def fallback_score(job: Job, profile: UserProfile) -> ScoreResult:
    text = normalize(" ".join([job.title, job.company, job.location, job.remote_policy, job.description]))
    score = 0
    reasons: List[str] = []
    concerns: List[str] = []
    if any(word_boundary_search(title, job.title) for title in profile.target_titles):
        score += 30
        reasons.append("profile_title: matched target title")
    keyword_hits = [keyword for keyword in profile.positive_keywords if word_boundary_search(keyword, text)]
    if keyword_hits:
        score += min(35, 10 * len(keyword_hits))
        reasons.append("profile_keywords: matched %s" % ", ".join(keyword_hits[:4]))
    if job.remote_policy == "remote":
        score += 15
        reasons.append("remote_friendly: remote role")
    if any(word_boundary_search(term, text) for term in profile.negative_keywords + profile.excluded_domains):
        return ScoreResult(score=0, hard_reject=True, concerns=["profile_exclusion: matched excluded term"])
    if not reasons:
        concerns.append("No baseline profile terms matched")
    return ScoreResult(score=min(100, score), hard_reject=False, reasons=reasons, concerns=concerns)


def reasons_from_row(row) -> List[str]:
    return _json_list(row["reasons_json"] if "reasons_json" in row.keys() else None)


def concerns_from_row(row) -> List[str]:
    return _json_list(row["concerns_json"] if "concerns_json" in row.keys() else None)


def _json_list(value) -> List[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
    except Exception:
        pass
    return []


def dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
