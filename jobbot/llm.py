import json
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from .budget import BudgetGate
from .config import AppConfig
from .models import UserProfile


class LLMClient:
    def __init__(self, config: AppConfig, budget: BudgetGate):
        self.config = config
        self.budget = budget

    def generate(self, task: str, prompt: str, max_output_tokens: int = 700) -> Optional[str]:
        if not self.config.openai_api_key:
            return None
        estimate = self.budget.estimate(prompt, max_output_tokens)
        if not self.budget.can_spend(estimate):
            return None

        payload = {
            "model": self.config.openai_model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % self.config.openai_api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError:
            return None
        data = json.loads(raw)
        text = extract_response_text(data)
        self.budget.record(task, self.config.openai_model, estimate, text or "")
        return text

    def cover_note(self, profile: UserProfile, job_row) -> str:
        prompt = """Write a concise, accurate job application cover note.

Constraints:
- 120 to 220 words.
- Do not invent experience.
- Use a direct, specific tone.
- Mention concrete fit between the candidate and the role.
- Do not claim the candidate has applied.

Candidate profile:
%s

Job:
Title: %s
Company: %s
Location: %s
Description:
%s
""" % (
            profile.raw_text[:6000],
            job_row["title"],
            job_row["company"],
            job_row["location"] or "",
            (job_row["description"] or "")[:6000],
        )
        generated = self.generate("cover_note", prompt, max_output_tokens=500)
        if generated:
            return generated.strip()
        return fallback_cover_note(profile, job_row)

    def source_discovery(self, profile: UserProfile, metrics: str) -> str:
        prompt = """Find high-signal job sources for this candidate profile.

Avoid LinkedIn logged-in scraping or any source requiring browser cookies.
Prioritize public APIs, RSS feeds, company career pages, ATS pages, startup boards,
VC portfolio hiring pages, communities, and safe email alerts.

Return a compact Markdown table with:
source_name | source_type | access_method | why_it_matches | risk | first_query_or_url

Candidate profile:
%s

Structured preferences:
%s

Recent source metrics:
%s
""" % (
            profile.raw_text[:6000],
            profile_summary(profile),
            metrics[:4000],
        )
        generated = self.generate("source_discovery", prompt, max_output_tokens=900)
        if generated:
            return generated.strip()
        return fallback_source_discovery()


def extract_response_text(data: Dict) -> str:
    if "output_text" in data and data["output_text"]:
        return str(data["output_text"])
    parts: List[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                parts.append(content.get("text", ""))
    return "\n".join(part for part in parts if part)


def fallback_cover_note(profile: UserProfile, job_row) -> str:
    skills = ", ".join(profile.positive_keywords[:5]) or "the requirements in the role"
    return (
        "Hi,\n\n"
        "I am interested in the %s role at %s. The role looks like a strong match for my background, "
        "especially around %s. I like that the position appears to combine practical execution with "
        "ownership, and I would be keen to discuss how my experience could help the team move faster.\n\n"
        "Best,\n"
    ) % (job_row["title"], job_row["company"], skills)


def fallback_source_discovery() -> str:
    return """| source_name | source_type | access_method | why_it_matches | risk | first_query_or_url |
|---|---|---|---|---|---|
| Ashby company boards | ATS | Public web/search | High-quality startup roles and direct applications | Low | `site:jobs.ashbyhq.com "remote" "senior"` |
| Greenhouse boards | ATS | Public web/search | Broad company coverage and structured pages | Low | `site:boards.greenhouse.io "remote" "engineer"` |
| YC Work at a Startup | Startup board | Public web | Early-stage roles and founder-led hiring | Low | `https://www.ycombinator.com/jobs` |
| Hacker News Who is Hiring | Community thread | Public web/API | Fresh and often lower-competition opportunities | Low | `https://news.ycombinator.com/submitted?id=whoishiring` |
| VC portfolio career pages | Company lists | Public web | Companies with recent funding and hiring budget | Low | Search for target VC portfolio hiring pages |
"""


def profile_summary(profile: UserProfile) -> str:
    return json.dumps(
        {
            "target_titles": profile.target_titles,
            "positive_keywords": profile.positive_keywords,
            "negative_keywords": profile.negative_keywords,
            "required_locations": profile.required_locations,
            "excluded_locations": profile.excluded_locations,
            "excluded_domains": profile.excluded_domains,
            "salary_floor": profile.salary_floor,
            "currency": profile.currency,
        },
        indent=2,
    )
