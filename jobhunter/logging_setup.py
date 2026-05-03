import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_") and key != "_context":
                continue
            if key == "_context" and isinstance(value, dict):
                payload.update(mask_secrets(value))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(mask_secrets(payload), sort_keys=True)


def configure_logging() -> None:
    level_name = os.getenv("JOBHUNTER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def log_context(logger: logging.Logger, level: int, message: str, **context) -> None:
    logger.log(level, message, extra={"_context": context})


def safe_log_text(value, limit: int) -> str:
    text = str(value or "").replace("\x00", " ")
    text = " ".join(text.split())
    return text[:limit]


def mask_secrets(value):
    if isinstance(value, dict):
        return {key: mask_secrets_item(key, item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def mask_secrets_item(key: str, value):
    lower = key.lower()
    if any(token in lower for token in ("token", "secret", "password", "api_key", "authorization")):
        if value:
            return "***"
    return mask_secrets(value)
