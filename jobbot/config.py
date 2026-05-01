import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .models import SourceConfig, UserProfile


@dataclass
class CostConfig:
    daily_budget_usd: float = 0.50
    monthly_budget_usd: float = 10.00
    input_usd_per_million: float = 0.10
    output_usd_per_million: float = 0.40


@dataclass
class AppConfig:
    data_dir: Path
    input_dir: Path
    config_dir: Path
    database_path: Path
    profile_path: Path
    cv_path: Path
    profile_settings_path: Path
    sources_path: Path
    scoring_path: Path
    workspace_dir: Path
    heartbeat_path: Path
    telegram_bot_token: str = ""
    telegram_allowed_chat_id: Optional[int] = None
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    digest_max_jobs: int = 10
    collect_interval_minutes: int = 240
    max_llm_jobs_per_run: int = 30
    max_response_bytes: int = 8 * 1024 * 1024
    check_robots: bool = True
    rate_limit_collect_seconds: int = 600
    rate_limit_discovery_per_day: int = 3
    rate_limit_tuning_per_day: int = 3
    rate_limit_cover_notes_per_day: int = 10
    codex_handoff_mode: str = "auto"
    cost: CostConfig = field(default_factory=CostConfig)


def _cwd() -> Path:
    return Path(os.getcwd())


def _default_path(env_name: str, relative_path: str) -> Path:
    value = os.getenv(env_name)
    if value:
        return Path(value)
    return _cwd() / relative_path


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def env_or_setting(env_name: str, settings: Dict, key: str, default, cast):
    raw = os.getenv(env_name)
    if raw is None:
        raw = settings.get(key, default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def load_app_config() -> AppConfig:
    data_dir = _default_path("JOBBOT_DATA_DIR", "data")
    input_dir = _default_path("JOBBOT_INPUT_DIR", "input")
    config_dir = _default_path("JOBBOT_CONFIG_DIR", "config")
    profile_path = Path(os.getenv("JOBBOT_PROFILE_PATH", str(input_dir / "profile.local.md")))
    cv_path = Path(os.getenv("JOBBOT_CV_PATH", str(input_dir / "cv.local.md")))
    profile_settings_path = Path(
        os.getenv("JOBBOT_PROFILE_SETTINGS_PATH", str(config_dir / "profile.local.json"))
    )
    sources_path = Path(os.getenv("JOBBOT_SOURCES_PATH", str(config_dir / "sources.json")))
    scoring_path = Path(os.getenv("JOBBOT_SCORING_PATH", str(config_dir / "scoring.json")))
    workspace_dir = Path(os.getenv("JOBBOT_WORKSPACE_DIR", "openclaw/workspace"))
    heartbeat_path = Path(os.getenv("JOBBOT_HEARTBEAT_PATH", str(data_dir / "heartbeat")))
    database_path = Path(os.getenv("JOBBOT_DATABASE_PATH", str(data_dir / "jobs.sqlite")))

    settings_path = Path(os.getenv("JOBBOT_SETTINGS_PATH", str(config_dir / "jobbot.json")))
    settings = load_json(settings_path, {})

    cost_settings = settings.get("cost", {})
    cost = CostConfig(
        daily_budget_usd=env_or_setting("JOBBOT_DAILY_BUDGET_USD", cost_settings, "daily_budget_usd", 0.50, float),
        monthly_budget_usd=env_or_setting("JOBBOT_MONTHLY_BUDGET_USD", cost_settings, "monthly_budget_usd", 10.00, float),
        input_usd_per_million=env_or_setting(
            "JOBBOT_INPUT_USD_PER_MILLION", cost_settings, "input_usd_per_million", 0.15, float
        ),
        output_usd_per_million=env_or_setting(
            "JOBBOT_OUTPUT_USD_PER_MILLION", cost_settings, "output_usd_per_million", 0.60, float
        ),
    )

    return AppConfig(
        data_dir=data_dir,
        input_dir=input_dir,
        config_dir=config_dir,
        database_path=database_path,
        profile_path=profile_path,
        cv_path=cv_path,
        profile_settings_path=profile_settings_path,
        sources_path=sources_path,
        scoring_path=scoring_path,
        workspace_dir=workspace_dir,
        heartbeat_path=heartbeat_path,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_chat_id=parse_optional_int(os.getenv("TELEGRAM_ALLOWED_CHAT_ID")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", settings.get("openai_model", "gpt-4o-mini")),
        digest_max_jobs=env_or_setting("JOBBOT_DIGEST_MAX_JOBS", settings, "digest_max_jobs", 10, int),
        collect_interval_minutes=env_or_setting(
            "JOBBOT_COLLECT_INTERVAL_MINUTES", settings, "collect_interval_minutes", 240, int
        ),
        max_llm_jobs_per_run=env_or_setting("JOBBOT_MAX_LLM_JOBS_PER_RUN", settings, "max_llm_jobs_per_run", 30, int),
        max_response_bytes=env_or_setting("JOBBOT_MAX_RESPONSE_BYTES", settings, "max_response_bytes", 8 * 1024 * 1024, int),
        check_robots=env_or_setting("JOBBOT_CHECK_ROBOTS", settings, "check_robots", True, bool_from_value),
        rate_limit_collect_seconds=env_or_setting(
            "JOBBOT_RATE_LIMIT_COLLECT_SECONDS", settings, "rate_limit_collect_seconds", 600, int
        ),
        rate_limit_discovery_per_day=env_or_setting(
            "JOBBOT_RATE_LIMIT_DISCOVERY_PER_DAY", settings, "rate_limit_discovery_per_day", 3, int
        ),
        rate_limit_tuning_per_day=env_or_setting(
            "JOBBOT_RATE_LIMIT_TUNING_PER_DAY", settings, "rate_limit_tuning_per_day", 3, int
        ),
        rate_limit_cover_notes_per_day=env_or_setting(
            "JOBBOT_RATE_LIMIT_COVER_NOTES_PER_DAY", settings, "rate_limit_cover_notes_per_day", 10, int
        ),
        codex_handoff_mode=str(os.getenv("JOBBOT_CODEX_HANDOFF_MODE", settings.get("codex_handoff_mode", "auto"))).strip().lower(),
        cost=cost,
    )


def load_sources(path: Path) -> List[SourceConfig]:
    raw_sources = load_json(path, [])
    sources = []
    for raw in raw_sources:
        sources.append(
            SourceConfig(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                type=raw["type"],
                url=validate_source_url(raw["url"], raw.get("type", "")),
                status=raw.get("status") or ("active" if bool(raw.get("enabled", True)) else "disabled"),
                risk_level=raw.get("risk_level", "low"),
                poll_frequency_minutes=int(raw.get("poll_frequency_minutes", 360)),
                headers=raw.get("headers", {}),
                query=raw.get("query"),
                created_by=raw.get("created_by", "user"),
                imap_last_uid=int(raw.get("imap_last_uid", 0) or 0),
            )
        )
    return sources


def load_profile(config: AppConfig) -> UserProfile:
    profile_settings = load_json(config.profile_settings_path, None)
    if profile_settings is None:
        profile_settings = load_json(config.config_dir / "profile.example.json", {})
    raw_text = ""
    if config.profile_path.exists():
        raw_text = config.profile_path.read_text(encoding="utf-8")
    cv_text = ""
    if config.cv_path.exists():
        cv_text = config.cv_path.read_text(encoding="utf-8")
    parsed = parse_profile_description(raw_text)

    return UserProfile(
        raw_text=raw_text,
        cv_text=cv_text,
        target_titles=merge_lists(parsed["target_titles"], _list(profile_settings.get("target_titles"))),
        positive_keywords=merge_lists(parsed["positive_keywords"], _list(profile_settings.get("positive_keywords"))),
        negative_keywords=_list(profile_settings.get("negative_keywords")),
        required_locations=_list(profile_settings.get("required_locations")),
        excluded_locations=_list(profile_settings.get("excluded_locations")),
        excluded_domains=_list(profile_settings.get("excluded_domains")),
        salary_floor=profile_settings.get("salary_floor"),
        currency=profile_settings.get("currency", "USD"),
    )


def ensure_directories(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    (config.workspace_dir / "discovery").mkdir(parents=True, exist_ok=True)
    (config.workspace_dir / "tuning").mkdir(parents=True, exist_ok=True)


def _list(value: Optional[object]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def merge_lists(primary: List[str], secondary: List[str]) -> List[str]:
    result: List[str] = []
    for item in primary + secondary:
        normalized = " ".join(str(item).lower().split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def bool_from_value(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def validate_source_url(url: str, source_type: str = "") -> str:
    if str(source_type).lower() == "imap":
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Unsafe source URL scheme for %s" % url)
    return url


def parse_profile_description(text: str) -> Dict[str, List[str]]:
    lower = (text or "").lower()
    title_patterns = [
        r"\b(product manager|product lead|product owner|head of product|product builder|product engineer)\b",
        r"\b([a-z][a-z0-9 +#.-]{1,40}\s+(?:engineer|manager|lead|owner|builder|architect))\b",
        r"\b(head of [a-z][a-z0-9 +#.-]{1,30})\b",
    ]
    titles: List[str] = []
    for pattern in title_patterns:
        for match in re.finditer(pattern, lower):
            value = match.group(1).strip(" .,-")
            if 2 < len(value) < 80:
                titles.append(value)
    keyword_candidates = [
        "ai",
        "ai automation",
        "analytics",
        "claude",
        "codex",
        "discovery",
        "llm",
        "mvp",
        "product analytics",
        "prototype",
        "stakeholder",
        "automation",
    ]
    keywords = [keyword for keyword in keyword_candidates if keyword in lower]
    return {"target_titles": merge_lists(titles, []), "positive_keywords": merge_lists(keywords, [])}
