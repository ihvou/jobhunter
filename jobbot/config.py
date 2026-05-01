import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
    profile_settings_path: Path
    sources_path: Path
    telegram_bot_token: str = ""
    telegram_allowed_chat_id: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-nano"
    digest_max_jobs: int = 10
    collect_interval_minutes: int = 240
    max_llm_jobs_per_run: int = 30
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


def load_app_config() -> AppConfig:
    data_dir = _default_path("JOBBOT_DATA_DIR", "data")
    input_dir = _default_path("JOBBOT_INPUT_DIR", "input")
    config_dir = _default_path("JOBBOT_CONFIG_DIR", "config")
    profile_path = Path(os.getenv("JOBBOT_PROFILE_PATH", str(input_dir / "profile.local.md")))
    profile_settings_path = Path(
        os.getenv("JOBBOT_PROFILE_SETTINGS_PATH", str(config_dir / "profile.local.json"))
    )
    sources_path = Path(os.getenv("JOBBOT_SOURCES_PATH", str(config_dir / "sources.json")))
    database_path = Path(os.getenv("JOBBOT_DATABASE_PATH", str(data_dir / "jobs.sqlite")))

    settings_path = Path(os.getenv("JOBBOT_SETTINGS_PATH", str(config_dir / "jobbot.json")))
    settings = load_json(settings_path, {})

    cost_settings = settings.get("cost", {})
    cost = CostConfig(
        daily_budget_usd=float(os.getenv("JOBBOT_DAILY_BUDGET_USD", cost_settings.get("daily_budget_usd", 0.50))),
        monthly_budget_usd=float(os.getenv("JOBBOT_MONTHLY_BUDGET_USD", cost_settings.get("monthly_budget_usd", 10.00))),
        input_usd_per_million=float(cost_settings.get("input_usd_per_million", 0.10)),
        output_usd_per_million=float(cost_settings.get("output_usd_per_million", 0.40)),
    )

    return AppConfig(
        data_dir=data_dir,
        input_dir=input_dir,
        config_dir=config_dir,
        database_path=database_path,
        profile_path=profile_path,
        profile_settings_path=profile_settings_path,
        sources_path=sources_path,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_chat_id=os.getenv("TELEGRAM_ALLOWED_CHAT_ID", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", settings.get("openai_model", "gpt-5.4-nano")),
        digest_max_jobs=int(settings.get("digest_max_jobs", 10)),
        collect_interval_minutes=int(settings.get("collect_interval_minutes", 240)),
        max_llm_jobs_per_run=int(settings.get("max_llm_jobs_per_run", 30)),
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
                url=raw["url"],
                enabled=bool(raw.get("enabled", True)),
                risk_level=raw.get("risk_level", "low"),
                poll_frequency_minutes=int(raw.get("poll_frequency_minutes", 360)),
                headers=raw.get("headers", {}),
                query=raw.get("query"),
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

    return UserProfile(
        raw_text=raw_text,
        target_titles=_list(profile_settings.get("target_titles")),
        positive_keywords=_list(profile_settings.get("positive_keywords")),
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


def _list(value: Optional[object]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
