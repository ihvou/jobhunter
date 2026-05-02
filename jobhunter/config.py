import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .models import SourceConfig, UserProfile
from .sources import VALID_SOURCE_TYPES, normalize_source_type


class ConfigError(RuntimeError):
    pass


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
    tasks_path: Path
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
    rate_limit_agent_per_day: int = 20
    rate_limit_agent_seconds: int = 10
    rate_limit_sources_apply_per_day: int = 5
    rate_limit_scoring_apply_per_day: int = 3
    rate_limit_bulk_apply_per_day: int = 2
    l2_max_jobs: int = 30
    agent_request_recent_jobs: int = 15
    agent_request_desc_chars: int = 250
    agent_request_feedback_items: int = 5
    robots_txt_respect: str = "trust"
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
    data_dir = _default_path("JOBHUNTER_DATA_DIR", "data")
    input_dir = _default_path("JOBHUNTER_INPUT_DIR", "input")
    config_dir = _default_path("JOBHUNTER_CONFIG_DIR", "config")
    profile_path = Path(os.getenv("JOBHUNTER_PROFILE_PATH", str(input_dir / "profile.local.md")))
    cv_path = Path(os.getenv("JOBHUNTER_CV_PATH", str(input_dir / "cv.local.md")))
    profile_settings_path = Path(
        os.getenv("JOBHUNTER_PROFILE_SETTINGS_PATH", str(config_dir / "profile.local.json"))
    )
    sources_path = Path(os.getenv("JOBHUNTER_SOURCES_PATH", str(config_dir / "sources.json")))
    scoring_path = Path(os.getenv("JOBHUNTER_SCORING_PATH", str(config_dir / "scoring.json")))
    workspace_dir = Path(os.getenv("JOBHUNTER_WORKSPACE_DIR", "openclaw/workspace"))
    heartbeat_path = Path(os.getenv("JOBHUNTER_HEARTBEAT_PATH", str(data_dir / "heartbeat")))
    database_path = Path(os.getenv("JOBHUNTER_DATABASE_PATH", str(data_dir / "jobs.sqlite")))
    tasks_default = "/jobhunter/repo/tasks.md" if Path("/jobhunter/repo").exists() else str(_cwd() / "tasks.md")
    tasks_path = Path(os.getenv("JOBHUNTER_TASKS_PATH", tasks_default))

    settings_path = Path(os.getenv("JOBHUNTER_SETTINGS_PATH", str(config_dir / "jobhunter.json")))
    settings = load_json(settings_path, {})

    cost_settings = settings.get("cost", {})
    cost = CostConfig(
        daily_budget_usd=env_or_setting("JOBHUNTER_DAILY_BUDGET_USD", cost_settings, "daily_budget_usd", 0.50, float),
        monthly_budget_usd=env_or_setting("JOBHUNTER_MONTHLY_BUDGET_USD", cost_settings, "monthly_budget_usd", 10.00, float),
        input_usd_per_million=env_or_setting(
            "JOBHUNTER_INPUT_USD_PER_MILLION", cost_settings, "input_usd_per_million", 0.15, float
        ),
        output_usd_per_million=env_or_setting(
            "JOBHUNTER_OUTPUT_USD_PER_MILLION", cost_settings, "output_usd_per_million", 0.60, float
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
        tasks_path=tasks_path,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_chat_id=parse_optional_int(os.getenv("TELEGRAM_ALLOWED_CHAT_ID")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", settings.get("openai_model", "gpt-4o-mini")),
        digest_max_jobs=env_or_setting("JOBHUNTER_DIGEST_MAX_JOBS", settings, "digest_max_jobs", 10, int),
        collect_interval_minutes=env_or_setting(
            "JOBHUNTER_COLLECT_INTERVAL_MINUTES", settings, "collect_interval_minutes", 240, int
        ),
        max_llm_jobs_per_run=env_or_setting("JOBHUNTER_MAX_LLM_JOBS_PER_RUN", settings, "max_llm_jobs_per_run", 30, int),
        max_response_bytes=env_or_setting("JOBHUNTER_MAX_RESPONSE_BYTES", settings, "max_response_bytes", 8 * 1024 * 1024, int),
        check_robots=env_or_setting("JOBHUNTER_CHECK_ROBOTS", settings, "check_robots", True, bool_from_value),
        rate_limit_collect_seconds=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_COLLECT_SECONDS", settings, "rate_limit_collect_seconds", 600, int
        ),
        rate_limit_discovery_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_DISCOVERY_PER_DAY", settings, "rate_limit_discovery_per_day", 3, int
        ),
        rate_limit_tuning_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_TUNING_PER_DAY", settings, "rate_limit_tuning_per_day", 3, int
        ),
        rate_limit_cover_notes_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_COVER_NOTES_PER_DAY", settings, "rate_limit_cover_notes_per_day", 10, int
        ),
        rate_limit_agent_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_AGENT_PER_DAY", settings, "rate_limit_agent_per_day", 20, int
        ),
        rate_limit_agent_seconds=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_AGENT_SECONDS", settings, "rate_limit_agent_seconds", 10, int
        ),
        rate_limit_sources_apply_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_SOURCES_APPLY_PER_DAY", settings, "rate_limit_sources_apply_per_day", 5, int
        ),
        rate_limit_scoring_apply_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_SCORING_APPLY_PER_DAY", settings, "rate_limit_scoring_apply_per_day", 3, int
        ),
        rate_limit_bulk_apply_per_day=env_or_setting(
            "JOBHUNTER_RATE_LIMIT_BULK_APPLY_PER_DAY", settings, "rate_limit_bulk_apply_per_day", 2, int
        ),
        l2_max_jobs=env_or_setting("JOBHUNTER_L2_MAX_JOBS", settings, "l2_max_jobs", 30, int),
        agent_request_recent_jobs=env_or_setting(
            "JOBHUNTER_AGENT_REQUEST_RECENT_JOBS", settings, "agent_request_recent_jobs", 15, int
        ),
        agent_request_desc_chars=env_or_setting(
            "JOBHUNTER_AGENT_REQUEST_DESC_CHARS", settings, "agent_request_desc_chars", 250, int
        ),
        agent_request_feedback_items=env_or_setting(
            "JOBHUNTER_AGENT_REQUEST_FEEDBACK_ITEMS", settings, "agent_request_feedback_items", 5, int
        ),
        robots_txt_respect=str(
            os.getenv("JOBHUNTER_ROBOTS_TXT_RESPECT", settings.get("robots_txt_respect", "trust"))
        ).strip().lower(),
        codex_handoff_mode=str(os.getenv("JOBHUNTER_CODEX_HANDOFF_MODE", settings.get("codex_handoff_mode", "auto"))).strip().lower(),
        cost=cost,
    )


def load_sources(path: Path) -> List[SourceConfig]:
    raw_sources = load_json(path, [])
    sources = []
    for raw in raw_sources:
        source_type = normalize_config_source_type(raw.get("type"), raw.get("id") or raw.get("name") or raw.get("url"))
        sources.append(
            SourceConfig(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                type=source_type,
                url=validate_source_url(raw["url"], source_type),
                status=raw.get("status") or ("active" if bool(raw.get("enabled", True)) else "disabled"),
                risk_level=raw.get("risk_level", "low"),
                poll_frequency_minutes=int(raw.get("poll_frequency_minutes", 360)),
                headers=raw.get("headers", {}),
                query=raw.get("query"),
                priority=raw.get("priority", "medium") if raw.get("priority") in ("high", "medium", "low") else "medium",
                created_by=raw.get("created_by", "user"),
                imap_last_uid=int(raw.get("imap_last_uid", 0) or 0),
                robots_check=parse_optional_bool(raw.get("robots_check")),
            )
        )
    return sources


def load_profile(config: AppConfig) -> UserProfile:
    raw_text = ""
    if config.profile_path.exists():
        raw_text = config.profile_path.read_text(encoding="utf-8")
    sections = split_profile_sections(raw_text)
    cv_text = ""
    if config.cv_path.exists():
        cv_text = config.cv_path.read_text(encoding="utf-8")
    parsed = parse_profile_description(raw_text)

    return UserProfile(
        raw_text=raw_text,
        cv_text=cv_text,
        about_me=sections["about_me"],
        directives=sections["directives"],
        target_titles=parsed["target_titles"],
        positive_keywords=parsed["positive_keywords"],
        negative_keywords=[],
        required_locations=[],
        excluded_locations=[],
        excluded_domains=[],
        salary_floor=None,
        currency="USD",
    )


def ensure_directories(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    (config.workspace_dir / "discovery").mkdir(parents=True, exist_ok=True)
    (config.workspace_dir / "tuning").mkdir(parents=True, exist_ok=True)
    (config.workspace_dir / "agent").mkdir(parents=True, exist_ok=True)


def ensure_profile_file(config: AppConfig) -> None:
    config.input_dir.mkdir(parents=True, exist_ok=True)
    if not config.profile_path.exists():
        copy_example_or_empty(config.input_dir / "profile.example.md", config.profile_path, "# About me\n\n# Directives\n")
    if not config.cv_path.exists():
        example_cv = config.input_dir / "cv.example.md"
        if example_cv.exists():
            shutil.copyfile(example_cv, config.cv_path)
    legacy = load_json(config.profile_settings_path, None)
    if legacy:
        raw = config.profile_path.read_text(encoding="utf-8")
        sections = split_profile_sections(raw)
        folded = legacy_profile_text(legacy)
        if folded and folded not in sections["about_me"]:
            archive = config.profile_settings_path.with_suffix(config.profile_settings_path.suffix + ".bak")
            shutil.copyfile(config.profile_settings_path, archive)
            config.profile_path.write_text(
                compose_profile("\n\n".join(part for part in [sections["about_me"], folded] if part.strip()), sections["directives"]),
                encoding="utf-8",
            )
            config.profile_settings_path.unlink()
    if not config.profile_path.exists():
        return
    raw_text = config.profile_path.read_text(encoding="utf-8")
    if "# About me" not in raw_text and "# Directives" not in raw_text:
        config.profile_path.write_text("# About me\n\n%s\n\n# Directives\n" % raw_text.strip(), encoding="utf-8")


def split_profile_sections(text: str) -> Dict[str, str]:
    text = text or ""
    if "# About me" not in text and "# Directives" not in text:
        return {"about_me": text.strip(), "directives": ""}
    about = ""
    directives = ""
    current = None
    buffers = {"about_me": [], "directives": []}
    for line in text.splitlines():
        normalized = line.strip().lower()
        if normalized == "# about me":
            current = "about_me"
            continue
        if normalized == "# directives":
            current = "directives"
            continue
        if current in buffers:
            buffers[current].append(line)
    about = "\n".join(buffers["about_me"]).strip()
    directives = "\n".join(buffers["directives"]).strip()
    return {"about_me": about, "directives": directives}


def compose_profile(about_me: str, directives: str) -> str:
    return "# About me\n\n%s\n\n# Directives\n%s\n" % ((about_me or "").strip(), ("\n" + directives.strip()) if directives else "")


def legacy_profile_text(profile_settings: Dict) -> str:
    lines = []
    for key, label in [
        ("target_titles", "Target titles"),
        ("positive_keywords", "Positive keywords"),
        ("negative_keywords", "Negative keywords"),
        ("required_locations", "Required locations"),
        ("excluded_locations", "Excluded locations"),
        ("excluded_domains", "Excluded domains"),
    ]:
        values = _list(profile_settings.get(key))
        if values:
            lines.append("%s: %s" % (label, ", ".join(values)))
    if profile_settings.get("salary_floor"):
        lines.append("Salary floor: %s %s" % (profile_settings.get("salary_floor"), profile_settings.get("currency", "USD")))
    return "\n".join(lines)


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


def parse_optional_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def bool_from_value(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_config_source_type(value, source_label: str) -> str:
    source_type = normalize_source_type(value)
    if source_type not in VALID_SOURCE_TYPES:
        allowed = "/".join(sorted(VALID_SOURCE_TYPES))
        raise ConfigError("Source '%s' has invalid type '%s'; allowed: %s" % (source_label, value, allowed))
    return source_type


def validate_source_url(url: str, source_type: str = "") -> str:
    if str(source_type).lower() == "imap":
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Unsafe source URL scheme for %s" % url)
    return url


def copy_example_or_empty(example_path: Path, target_path: Path, fallback: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if example_path.exists():
        shutil.copyfile(example_path, target_path)
    else:
        target_path.write_text(fallback, encoding="utf-8")


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
