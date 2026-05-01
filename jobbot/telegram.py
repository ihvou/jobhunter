import json
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .models import TelegramAction
from .scoring import concerns_from_row, reasons_from_row


class TelegramClient:
    def __init__(self, token: str, allowed_chat_id: str):
        self.token = token
        self.allowed_chat_id = str(allowed_chat_id or "")
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.allowed_chat_id)

    def send_job(self, row) -> None:
        text = format_job_message(row)
        self.send_message(text, reply_markup=job_keyboard(row["id"]))

    def send_message(self, text: str, reply_markup: Optional[Dict] = None) -> None:
        if not self.enabled:
            print(text)
            return
        payload = {
            "chat_id": self.allowed_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        self._post("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str) -> None:
        if not self.enabled:
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
            if str(chat_id) != self.allowed_chat_id:
                self.answer_callback(callback.get("id", ""), "Unauthorized chat")
                continue
            action = parse_callback(callback.get("data", ""))
            if not action:
                self.answer_callback(callback.get("id", ""), "Unknown action")
                continue
            action.callback_id = callback.get("id")
            action.chat_id = chat_id
            action.message_id = message.get("message_id")
            action.raw = callback
            actions.append(action)
        return actions

    def _post(self, method: str, payload: Dict) -> Dict:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(self._url(method), data=body, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, method_with_query: str) -> Dict:
        with urllib.request.urlopen(self._url(method_with_query), timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _url(self, method: str) -> str:
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)


def parse_callback(data: str) -> Optional[TelegramAction]:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "job":
        return None
    return TelegramAction(action=parts[1], job_id=parts[2])


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


def format_job_message(row) -> str:
    reasons = reasons_from_row(row)
    concerns = concerns_from_row(row)
    reason_text = "\n".join("- %s" % reason for reason in reasons[:4]) or "- Match details unavailable"
    concern_text = "\n".join("- %s" % concern for concern in concerns[:3]) or "- None flagged"
    return """%s - %s
Score: %s
Source: %s
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
        row["location"] or row["remote_policy"] or "Unknown",
        reason_text,
        concern_text,
        row["url"],
    )

