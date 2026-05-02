import json
import logging
import re
import time
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


BOT_MESSAGE_ACTIONS = {
    "get more jobs": "collect",
    "jobs": "collect",
    "/jobs": "collect",
    "/get_more_jobs": "collect",
    "update sources": "discover_sources",
    "sources": "discover_sources",
    "/sources": "discover_sources",
    "/update_sources": "discover_sources",
    "/discover_sources": "discover_sources",
    "tune scoring": "tune_scoring",
    "tune": "tune_scoring",
    "/tune": "tune_scoring",
    "/tune_scoring": "tune_scoring",
    "/scoring": "tune_scoring",
    "usage": "usage",
    "/usage": "usage",
    "/history": "history",
    "/applied": "list_applied",
    "/snoozed": "list_snoozed",
    "/irrelevant": "list_irrelevant",
    "menu": "menu",
    "/start": "menu",
    "/help": "menu",
}


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
        self.send_message(text, reply_markup=main_menu_keyboard())

    def send_job(self, row) -> None:
        self.send_message(format_job_message(row), reply_markup=job_keyboard(row["id"]), parse_mode="MarkdownV2")

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

    def send_agent_response(self, session_id: str, response: Dict) -> None:
        lines = [
            response.get("answer") or "Agent response ready.",
            "",
        ]
        actions = response.get("proposed_actions") or []
        write_actions = [action for action in actions if action.get("kind") != "data_answer"]
        if actions:
            lines.append("Proposed actions:")
            for idx, action in enumerate(actions):
                lines.append("%s. %s: %s" % (idx + 1, action.get("kind"), action.get("summary", "")))
        usage = response.get("usage") or {}
        footer = "Audit: session %s" % session_id
        if usage:
            footer += " · Codex turns: %s · SQL: %s · fetches: %s · duration: %ss" % (
                usage.get("codex_turns", 0),
                usage.get("sql_queries", 0),
                usage.get("http_fetches", 0),
                usage.get("duration_seconds", 0),
            )
        lines.extend(["", footer])
        self.send_message("\n".join(lines), reply_markup=agent_actions_keyboard(session_id, actions) if write_actions else main_menu_keyboard())

    def delete_message(self, message_id: Optional[int]) -> None:
        if not self.enabled or not message_id:
            return
        try:
            self._post("deleteMessage", {"chat_id": str(self.allowed_chat_id), "message_id": str(message_id)})
        except TelegramError as exc:
            log_context(LOGGER, logging.WARNING, "telegram_delete_message_failed", error=str(exc), message_id=message_id)

    def send_message(self, text: str, reply_markup: Optional[Dict] = None, parse_mode: Optional[str] = None) -> None:
        if not self.enabled:
            print(text)
            return
        payload = {
            "chat_id": str(self.allowed_chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        self._post("sendMessage", payload)

    def send_long_message(self, text: str) -> None:
        max_len = 3600
        chunks = split_message(text, max_len)
        for idx, chunk in enumerate(chunks):
            prefix = "Part %s/%s\n" % (idx + 1, len(chunks)) if len(chunks) > 1 else ""
            self.send_message(prefix + chunk)

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
            if callback:
                action = self._action_from_callback(callback)
                if action:
                    actions.append(action)
                continue
            message = update.get("message")
            if message:
                action = self._action_from_message(message)
                if action:
                    actions.append(action)
        return actions

    def _action_from_callback(self, callback: Dict) -> Optional[TelegramAction]:
        message = callback.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None or int(chat_id) != int(self.allowed_chat_id):
            self.answer_callback(callback.get("id", ""), "Unauthorized chat")
            return None
        action = parse_callback(callback.get("data", ""))
        if not action:
            self.answer_callback(callback.get("id", ""), "Unknown action")
            return None
        action.callback_id = callback.get("id")
        action.chat_id = int(chat_id)
        action.message_id = message.get("message_id")
        action.raw = callback
        return action

    def _action_from_message(self, message: Dict) -> Optional[TelegramAction]:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None or int(chat_id) != int(self.allowed_chat_id):
            return None
        action = parse_message(message.get("text", ""))
        if not action:
            self.send_message(
                "Use the keyboard buttons, or type /jobs, /sources, /tune, or /usage.",
                reply_markup=main_menu_keyboard(),
            )
            return None
        action.chat_id = int(chat_id)
        action.message_id = message.get("message_id")
        action.raw = message
        return action

    def _post(self, method: str, payload: Dict) -> Dict:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self._url(method), data=body, method="POST")
        data = self._urlopen_json(request, method)
        return self._check_ok(method, data)

    def _get(self, method_with_query: str) -> Dict:
        method = method_with_query.split("?")[0]
        data = self._urlopen_json(self._url(method_with_query), method)
        return self._check_ok(method, data)

    def _urlopen_json(self, request, method: str) -> Dict:
        last_error = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.URLError as exc:
                last_error = exc
                log_context(
                    LOGGER,
                    logging.WARNING if attempt == 0 else logging.ERROR,
                    "telegram_url_error",
                    method=method,
                    error=str(exc.reason),
                    attempt=attempt + 1,
                )
                if attempt == 0:
                    time.sleep(1)
        raise TelegramError("Telegram request failed for %s: %s" % (method, last_error.reason))

    def _check_ok(self, method: str, data: Dict) -> Dict:
        if data.get("ok", True) is False:
            log_context(LOGGER, logging.ERROR, "telegram_api_error", method=method, description=data.get("description"))
            raise TelegramError("Telegram API error for %s: %s" % (method, data.get("description", "unknown error")))
        return data

    def _url(self, method: str) -> str:
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)


def parse_message(text: str) -> Optional[TelegramAction]:
    raw = str(text or "").strip()
    command, rest = split_command(raw)
    if command in ("/agent", "/feedback", "/ask"):
        hint = {
            "/agent": "",
            "/feedback": "User feedback: ",
            "/ask": "Answer this question using allowed read-only tools: ",
        }[command]
        if not rest:
            return TelegramAction(scope="bot", action="agent_help")
        return TelegramAction(scope="bot", action="agent", text=hint + rest)
    if command == "/revert":
        return TelegramAction(scope="bot", action="revert", target_id=rest.strip())
    if command == "/profile":
        if not rest:
            return TelegramAction(scope="profile", action="show")
        profile_command, profile_rest = split_first_word(rest)
        if profile_command in ("show", "set", "refine"):
            return TelegramAction(scope="profile", action=profile_command, text=profile_rest)
        return TelegramAction(scope="profile", action="show")
    if command == "/scoring":
        sub, _tail = split_first_word(rest)
        if sub == "history":
            return TelegramAction(scope="bot", action="scoring_history")
    normalized = normalize_message_text(text)
    action = BOT_MESSAGE_ACTIONS.get(normalized)
    if not action:
        return None
    return TelegramAction(scope="bot", action=action)


def normalize_message_text(text: str) -> str:
    text = " ".join(str(text or "").strip().split())
    if not text:
        return ""
    if text.startswith("/"):
        command = text.split()[0].split("@", 1)[0]
        return command.lower()
    return text.lower()


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
    if parts[0] == "agent" and len(parts) == 4:
        index = None if parts[3] == "all" else int(parts[3]) if parts[3].isdigit() else None
        return TelegramAction(scope="agent", action=parts[1], target_id=parts[2], index=index)
    if parts[0] == "profile" and len(parts) == 3:
        return TelegramAction(scope="profile", action=parts[1], target_id=parts[2])
    return None


def main_menu_keyboard() -> Dict:
    return {
        "keyboard": [
            [{"text": "Get more jobs"}, {"text": "Update sources"}],
            [{"text": "Tune scoring"}, {"text": "Usage"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Choose an action",
    }


def digest_keyboard() -> Dict:
    return main_menu_keyboard()


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


def agent_actions_keyboard(session_id: str, actions: List[Dict]) -> Dict:
    rows = []
    current = []
    for idx, action in enumerate(actions):
        if action.get("kind") == "data_answer":
            continue
        current.append({"text": "Apply %s" % (idx + 1), "callback_data": "agent:apply:%s:%s" % (session_id, idx)})
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append(
        [
            {"text": "Apply all", "callback_data": "agent:apply:%s:all" % session_id},
            {"text": "Reject all", "callback_data": "agent:reject:%s:all" % session_id},
        ]
    )
    return {"inline_keyboard": rows}


def format_job_message(row) -> str:
    reasons = reasons_from_row(row)
    reason = row["l2_reason"] if "l2_reason" in row.keys() and row["l2_reason"] else (reasons[0] if reasons else "Match details unavailable")
    title = strip_company_prefix(row["title"], row["company"])
    new_source = " (from new source)" if row["source_status"] == "test" else ""
    priority = ""
    if "l2_priority" in row.keys() and row["l2_priority"] == "high":
        priority = "High priority · "
    excerpt = strip_html(row["description"] if "description" in row.keys() else "")[:250]
    return """*%s* — %s
Score %s · %s%s · %s%s

%s

> %s

%s""" % (
        mdv2_escape(title),
        mdv2_escape(row["company"]),
        row["score"],
        mdv2_escape(row["location"] or row["remote_policy"] or "Unknown"),
        mdv2_escape(new_source),
        mdv2_escape(row["source_name"]),
        "",
        mdv2_escape(priority + reason),
        mdv2_escape(excerpt or "No description excerpt available."),
        mdv2_escape(row["url"]),
    )


def split_command(text: str):
    if not text.startswith("/"):
        return "", text
    first, rest = split_first_word(text)
    return first.split("@", 1)[0].lower(), rest


def split_first_word(text: str):
    parts = str(text or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def strip_company_prefix(title: str, company: str) -> str:
    title = str(title or "").strip()
    company = str(company or "").strip()
    if company and title.lower().startswith(company.lower() + ":"):
        return title[len(company) + 1 :].strip()
    if company and title.lower().startswith(company.lower() + " - "):
        return title[len(company) + 3 :].strip()
    return title


def strip_html(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(text or "")).split())


def mdv2_escape(text) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text or ""))


def split_message(text: str, max_len: int) -> List[str]:
    text = str(text or "")
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        while len(line) + 1 > max_len:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:max_len])
            line = line[max_len:]
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]
