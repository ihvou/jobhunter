import json
import re
from typing import Dict, Iterable, List

from .models import Job, ScoreResult, UserProfile


def score_job(job: Job, profile: UserProfile) -> ScoreResult:
    text = normalize(" ".join([job.title, job.company, job.location, job.remote_policy, job.description]))
    title_text = normalize(job.title)
    breakdown: Dict[str, int] = {}
    reasons: List[str] = []
    concerns: List[str] = []
    hard_reject = False

    excluded_hit = first_hit(text, profile.excluded_domains + profile.negative_keywords)
    if excluded_hit:
        hard_reject = True
        concerns.append("Matches excluded term: %s" % excluded_hit)

    excluded_location_hit = first_hit(normalize(job.location), profile.excluded_locations)
    if excluded_location_hit:
        hard_reject = True
        concerns.append("Excluded location: %s" % excluded_location_hit)

    title_score, title_reason = score_terms(title_text, profile.target_titles, 20, "title")
    breakdown["role_title"] = title_score
    if title_reason:
        reasons.append(title_reason)

    skill_score, skill_reasons = score_keyword_overlap(text, profile.positive_keywords, 20)
    breakdown["skill_fit"] = skill_score
    reasons.extend(skill_reasons[:3])

    location_score = 0
    if job.remote_policy == "remote":
        location_score += 10
        reasons.append("Remote-compatible role")
    if profile.required_locations:
        location_hit = first_hit(text, profile.required_locations)
        if location_hit:
            location_score += 5
            reasons.append("Location/timezone match: %s" % location_hit)
        elif job.remote_policy != "remote":
            concerns.append("Location fit is unclear")
    else:
        location_score += 5
    breakdown["location"] = min(location_score, 15)

    seniority_score = seniority_fit(title_text + " " + text)
    breakdown["seniority"] = seniority_score
    if seniority_score > 0:
        reasons.append("Seniority appears compatible")

    salary_score = salary_fit(job, profile)
    breakdown["salary"] = salary_score
    if profile.salary_floor and not job.salary_min and not job.salary_max:
        concerns.append("Salary not listed")
    elif salary_score > 0:
        reasons.append("Compensation appears compatible")

    freshness_score = 5 if job.posted_at else 2
    breakdown["freshness"] = freshness_score

    actionability = 5 if job.url else 0
    breakdown["actionability"] = actionability

    data_quality = 0
    if len(job.description or "") > 400:
        data_quality += 3
    if job.company and job.company != "Unknown company":
        data_quality += 2
    breakdown["data_quality"] = data_quality

    total = sum(breakdown.values())
    if hard_reject:
        total = min(total, 20)
    total = max(0, min(100, total))

    if not reasons and not hard_reject:
        reasons.append("Basic metadata matched enough to keep for review")
    if job.remote_policy == "unknown":
        concerns.append("Remote policy is unclear")

    return ScoreResult(
        score=total,
        hard_reject=hard_reject,
        reasons=dedupe(reasons)[:5],
        concerns=dedupe(concerns)[:4],
        breakdown=breakdown,
    )


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def score_terms(text: str, terms: Iterable[str], max_score: int, label: str):
    term_list = [normalize(term) for term in terms if normalize(term)]
    if not term_list:
        return int(max_score * 0.4), ""
    for term in term_list:
        if term in text:
            return max_score, "Matched target %s: %s" % (label, term)
    return 0, ""


def score_keyword_overlap(text: str, keywords: Iterable[str], max_score: int):
    terms = [normalize(term) for term in keywords if normalize(term)]
    if not terms:
        return int(max_score * 0.3), []
    hits = [term for term in terms if term in text]
    if not hits:
        return 0, []
    score = int(max_score * min(1.0, len(hits) / max(3, len(terms) * 0.35)))
    return score, ["Skill/domain match: %s" % hit for hit in hits[:5]]


def seniority_fit(text: str) -> int:
    positive = ["senior", "staff", "principal", "founding", "lead", "architect"]
    negative = ["junior", "intern", "graduate", "entry level"]
    if first_hit(text, negative):
        return 0
    if first_hit(text, positive):
        return 10
    return 4


def salary_fit(job: Job, profile: UserProfile) -> int:
    if not profile.salary_floor:
        return 6
    salary_values = [value for value in [job.salary_min, job.salary_max] if value]
    if not salary_values:
        return 2
    if max(salary_values) >= profile.salary_floor:
        return 10
    return 0


def first_hit(text: str, terms: Iterable[str]):
    normalized = normalize(text)
    for term in terms:
        clean = normalize(term)
        if clean and clean in normalized:
            return term
    return None


def dedupe(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


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

