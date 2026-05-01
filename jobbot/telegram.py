import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .logging_setup import log_context
from .models import TelegramAction
from .scoring import concerns_from_row, reasons_from_row

LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, allowed_chat_id: Optional[int]):
        self.token = token or ""
        self.allowed_chat_id = allowed_chat_id
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.allowed_chat_id is not None)

    def send_digest_header(self, title: str, body: str = "") -> None:
        text = title if not body else "%s\n%s" % (title, body)
        self.send_message(text, reply_markup=digest_keyboard())

    def send_job(self, row) -> None:
        self.send_message(format_job_message(row), reply_markup=job_keyboard(row["id"]))

    def send_cover_override_prompt(self, job_id: str, reason: str) -> None:
        self.send_message(
            "%s OpenAI budget exceeded for this cover note." % reason.capitalize(),
            reply_markup=cover_override_keyboard(job_id),
        )

    def send_discovery_approval(self, session_id: str, candidates: List[Dict]) -> None:
        lines = ["Source discovery candidates:"]
        for idx, candidate in enumerate(candidates):
            lines.append(
                "%s. %s (%s)\n%s\n%s"
                % (
                    idx + 1,
                    candidate.get("name", "Unnamed"),
                    candidate.get("type", "unknown"),
                    candidate.get("why_it_matches", ""),
                    candidate.get("url", ""),
                )
            )
        self.send_message("\n\n".join(lines), reply_markup=discovery_keyboard(session_id, len(candidates)))

    def send_tuning_approval(self, session_id: str, report: str) -> None:
        self.send_message("Scoring tuning proposal:\n\n%s" % report, reply_markup=tuning_keyboard(session_id))

    def send_message(self, text: str, reply_markup: Optional[Dict] = None) -> None:
        if not self.enabled:
            print(text)
            return
        payload = {
            "chat_id": str(self.allowed_chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        self._post("sendMessage", payload)

    def answer_callback(self, callback_id: Optional[str], text: str) -> None:
        if not self.enabled or not callback_id:
            return
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def poll_actions(self) -> List[TelegramAction]:
        if not self.enabled:
            return []
        query = urllib.parse.urlencode({"timeout": 20, "offset": self.offset})
        data = self._get("getUpdates?%s" % query)
        actions = []
        for update in data.get("result", []):
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            callback = update.get("callback_query")
            if not callback:
                continue
            message = callback.get("message", {})
            chat = message.get("chat", {})
            chat_id = chat.get("id")
            if int(chat_id) != int(self.allowed_chat_id):
                self.answer_callback(callback.get("id", ""), "Unauthorized chat")
                continue
            action = parse_callback(callback.get("data", ""))
            if not action:
                self.answer_callback(callback.get("id", ""), "Unknown action")
                continue
            action.callback_id = callback.get("id")
            action.chat_id = int(chat_id)
            action.message_id = message.get("message_id")
            action.raw = callback
            actions.append(action)
        return actions

    def _post(self, method: str, payload: Dict) -> Dict:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self._url(method), data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            log_context(LOGGER, logging.ERROR, "telegram_url_error", method=method, error=str(exc.reason))
            raise TelegramError("Telegram request failed for %s: %s" % (method, exc.reason))
        return self._check_ok(method, data)

    def _get(self, method_with_query: str) -> Dict:
        try:
            with urllib.request.urlopen(self._url(method_with_query), timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            log_context(LOGGER, logging.ERROR, "telegram_url_error", method=method_with_query.split("?")[0], error=str(exc.reason))
            raise TelegramError("Telegram request failed: %s" % exc.reason)
        return self._check_ok(method_with_query.split("?")[0], data)

    def _check_ok(self, method: str, data: Dict) -> Dict:
        if data.get("ok", True) is False:
            log_context(LOGGER, logging.ERROR, "telegram_api_error", method=method, description=data.get("description"))
            raise TelegramError("Telegram API error for %s: %s" % (method, data.get("description", "unknown error")))
        return data

    def _url(self, method: str) -> str:
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)


def parse_callback(data: str) -> Optional[TelegramAction]:
    parts = (data or "").split(":")
    if not parts:
        return None
    if parts[0] == "bot" and len(parts) == 2:
        return TelegramAction(scope="bot", action=parts[1])
    if parts[0] == "job" and len(parts) == 3:
        return TelegramAction(scope="job", action=parts[1], target_id=parts[2])
    if parts[0] == "cover" and len(parts) == 3:
        return TelegramAction(scope="cover", action=parts[1], target_id=parts[2])
    if parts[0] == "disc" and len(parts) in (3, 4):
        index = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() else None
        return TelegramAction(scope="disc", action=parts[1], target_id=parts[2], index=index)
    if parts[0] == "tune" and len(parts) == 3:
        return TelegramAction(scope="tune", action=parts[1], target_id=parts[2])
    return None


def digest_keyboard() -> Dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Get more jobs", "callback_data": "bot:collect"},
                {"text": "Update sources", "callback_data": "bot:discover_sources"},
            ],
            [
                {"text": "Tune scoring", "callback_data": "bot:tune_scoring"},
                {"text": "Usage", "callback_data": "bot:usage"},
            ],
        ]
    }


def job_keyboard(job_id: str) -> Dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Irrelevant", "callback_data": "job:irrelevant:%s" % job_id},
                {"text": "Remind me tomorrow", "callback_data": "job:snooze_1d:%s" % job_id},
            ],
            [
                {"text": "Give me cover note", "callback_data": "job:cover_note:%s" % job_id},
                {"text": "Applied", "callback_data": "job:applied:%s" % job_id},
            ],
        ]
    }


def cover_override_keyboard(job_id: str) -> Dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Override once", "callback_data": "cover:override:%s" % job_id},
                {"text": "Cancel", "callback_data": "cover:cancel:%s" % job_id},
            ]
        ]
    }


def discovery_keyboard(session_id: str, count: int) -> Dict:
    row = [{"text": "Approve all", "callback_data": "disc:approve:%s:all" % session_id}]
    row.extend({"text": "Approve %s" % (idx + 1), "callback_data": "disc:approve:%s:%s" % (session_id, idx)} for idx in range(count))
    return {"inline_keyboard": [row[:3], row[3:], [{"text": "Reject all", "callback_data": "disc:reject:%s" % session_id}]]}


def tuning_keyboard(session_id: str) -> Dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Apply", "callback_data": "tune:apply:%s" % session_id},
                {"text": "Reject", "callback_data": "tune:reject:%s" % session_id},
                {"text": "Show diff", "callback_data": "tune:diff:%s" % session_id},
            ]
        ]
    }


def format_job_message(row) -> str:
    reasons = reasons_from_row(row)
    concerns = concerns_from_row(row)
    reason_text = "\n".join("- %s" % reason for reason in reasons[:5]) or "- Match details unavailable"
    concern_text = "\n".join("- %s" % concern for concern in concerns[:3]) or "- None flagged"
    new_source = " (from new source)" if row["source_status"] == "test" else ""
    return """%s - %s
Score: %s
Source: %s%s
Location: %s

Why it matches:
%s

Concerns:
%s

%s""" % (
        row["title"],
        row["company"],
        row["score"],
        row["source_name"],
        new_source,
        row["location"] or row["remote_policy"] or "Unknown",
        reason_text,
        concern_text,
        row["url"],
    )

