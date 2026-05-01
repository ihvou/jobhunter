from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class SourceConfig:
    id: str
    name: str
    type: str
    url: str
    enabled: bool = True
    risk_level: str = "low"
    poll_frequency_minutes: int = 360
    headers: Dict[str, str] = field(default_factory=dict)
    query: Optional[str] = None


@dataclass
class UserProfile:
    raw_text: str
    target_titles: List[str] = field(default_factory=list)
    positive_keywords: List[str] = field(default_factory=list)
    negative_keywords: List[str] = field(default_factory=list)
    required_locations: List[str] = field(default_factory=list)
    excluded_locations: List[str] = field(default_factory=list)
    excluded_domains: List[str] = field(default_factory=list)
    salary_floor: Optional[int] = None
    currency: str = "USD"


@dataclass
class Job:
    source_id: str
    source_name: str
    external_id: Optional[str]
    url: str
    title: str
    company: str
    location: str = ""
    remote_policy: str = "unknown"
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    currency: Optional[str] = None
    description: str = ""
    posted_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


@dataclass
class ScoreResult:
    score: int
    hard_reject: bool
    reasons: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass
class TelegramAction:
    action: str
    job_id: str
    callback_id: Optional[str] = None
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    raw: Dict = field(default_factory=dict)


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

