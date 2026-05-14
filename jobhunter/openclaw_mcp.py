import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict


SERVICE_URL = os.getenv("JOBHUNTER_SERVICE_URL", "http://127.0.0.1:8765").rstrip("/")


TOOLS = [
    {
        "name": "jobhunter_get_more_jobs",
        "description": (
            "Return ranked job matches from Jobhunter. "
            "STALENESS RULE (applies to Telegram digest requests like \"Get more jobs\", "
            "\"fresh\", \"new\", \"today\", \"latest\"): the response includes "
            "`queue_freshness_hours`, `queue_last_collected`, and `queue_is_stale`. "
            "If `queue_is_stale` is true OR `queue_freshness_hours` >= 6, you MUST first call "
            "`jobhunter_collect_all_sources` to pull fresh IMAP/RSS/ATS data, then call this "
            "tool AGAIN. Do not show a stale digest without refreshing. The collect roundtrip "
            "takes ~45-60s; that is expected. Tell the user briefly: \"Collecting fresh jobs, "
            "back in ~1 min.\" "
            "For read-only diagnostics, analysis, or source/scoring work, call with "
            "mark_sent=false and use the returned rows without sending channel messages. "
            "RENDERING (Telegram digest requests only): render EACH returned job with one "
            "`message` tool call (action=send, target=<chat_id from conversation metadata, e.g. "
            "\"telegram:855127987\">). Inline buttons MUST be sent as "
            "`presentation: {blocks: [{type: \"buttons\", buttons: [[...],[...]]}]}` with: "
            "[[{text:\"Applied\",callback_data:\"applied:<id_prefix>\",style:\"success\"},"
            "{text:\"Irrelevant\",callback_data:\"irrelevant:<id_prefix>\",style:\"danger\"}],"
            "[{text:\"Snooze\",callback_data:\"snooze:<id_prefix>\"},"
            "{text:\"Cover\",callback_data:\"cover:<id_prefix>\",style:\"primary\"}]] "
            "where <id_prefix> is the first 12 characters of the job's id (returned as "
            "id_prefix in each row). Do not replace the per-job messages with a single summary; "
            "use mark_sent=true only for rows the user is actually being shown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "mark_sent": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_collect_all_sources",
        "description": "Collect and index jobs from all enabled Jobhunter sources.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "jobhunter_usage",
        "description": "Return local spend, quota, and recent activity counters.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "jobhunter_history",
        "description": "Return recent approved/applied agent action audit rows.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_propose_actions",
        "description": (
            "Store bounded Jobhunter actions for user approval. This does not apply changes. "
            "For an Update sources request, propose kind=sources_proposal with payload.operations. "
            "For a Tune scoring request, propose kind=scoring_rule_proposal with payload.ruleset. "
            "After this returns action ids, show the ids to the user and only call "
            "jobhunter_apply_action after explicit approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "user_intent": {"type": "string"},
                "actions": {"type": "array"},
            },
            "required": ["actions"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_apply_action",
        "description": "Apply one previously proposed Jobhunter action after explicit user approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "integer"},
                "confirm": {"type": "boolean"},
            },
            "required": ["action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_revert_action",
        "description": "Revert a reversible Jobhunter action by audit id.",
        "inputSchema": {
            "type": "object",
            "properties": {"action_id": {"type": "integer"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_mark_job",
        "description": (
            "Mark a job as irrelevant, applied, or snoozed. Use job_id or the 12-char "
            "id_prefix from an inline button. Callback text such as `applied:<id_prefix>`, "
            "`irrelevant:<id_prefix>`, or `snooze:<id_prefix>` must be routed here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "id_prefix": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                "action": {"type": "string", "enum": ["irrelevant", "applied", "snooze", "snoozed"]},
                "status": {"type": "string", "enum": ["irrelevant", "applied", "snoozed"]},
                "details": {"type": "string"},
                "snooze_days": {"type": "integer", "minimum": 1, "maximum": 7},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_cover_note",
        "description": (
            "Draft a cover note for one job. Use job_id or the 12-char id_prefix from an "
            "inline button. Callback text `cover:<id_prefix>` must be routed here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "id_prefix": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                "override_budget": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_query_sql",
        "description": "Run a SELECT-only query against Jobhunter SQLite for investigation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "params": {"type": "array"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
]


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_rpc(request)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def handle_rpc(request: Dict):
    rpc_id = request.get("id")
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    try:
        if method == "initialize":
            return result(rpc_id, {"protocolVersion": "2024-11-05", "serverInfo": {"name": "jobhunter", "version": "1.0.0"}, "capabilities": {"tools": {}}})
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return result(rpc_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return result(rpc_id, {"content": [{"type": "text", "text": json.dumps(call_tool(name, args), sort_keys=True)}]})
        if method == "ping":
            return result(rpc_id, {})
        return error(rpc_id, -32601, "Method not found: %s" % method)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return error(rpc_id, exc.code, detail)
    except Exception as exc:
        return error(rpc_id, -32603, "%s: %s" % (exc.__class__.__name__, exc))


def call_tool(name: str, args: Dict) -> Dict:
    if name == "jobhunter_get_more_jobs":
        digest = post("/digest", {"limit": args.get("limit"), "mark_sent": bool(args.get("mark_sent", False))})
        return digest
    if name == "jobhunter_collect_all_sources":
        return post("/collect", {})
    if name == "jobhunter_usage":
        return get("/usage")
    if name == "jobhunter_history":
        return get("/history?limit=%s" % int(args.get("limit") or 10))
    if name == "jobhunter_propose_actions":
        return post(
            "/action/propose",
            {
                "session_id": args.get("session_id") or "",
                "user_intent": args.get("user_intent") or "",
                "actions": args.get("actions") or [],
            },
        )
    if name == "jobhunter_apply_action":
        return post("/action/apply", {"action_id": args.get("action_id"), "confirm": bool(args.get("confirm", False))})
    if name == "jobhunter_revert_action":
        return post("/action/revert", {"action_id": args.get("action_id")})
    if name == "jobhunter_mark_job":
        job_id = resolve_job_id(args)
        action = args.get("status") or args.get("action")
        if action == "irrelevant":
            return post("/irrelevant", {"job_id": job_id, "details": args.get("details", "")})
        if action == "applied":
            return post("/applied", {"job_id": job_id, "details": args.get("details", "")})
        if action in ("snooze", "snoozed"):
            return post("/snooze", {"job_id": job_id, "snooze_days": args.get("snooze_days") or 1})
        raise ValueError("Unsupported job action/status: %s" % action)
    if name == "jobhunter_cover_note":
        return post("/cover-note", {"job_id": resolve_job_id(args), "override_budget": bool(args.get("override_budget", False))})
    if name == "jobhunter_query_sql":
        return post("/query-sql", {"sql": args.get("sql"), "params": args.get("params") or [], "limit": args.get("limit") or 50})
    raise ValueError("Unknown tool: %s" % name)


def resolve_job_id(args: Dict) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if job_id:
        return job_id
    prefix = str(args.get("id_prefix") or "").strip().lower()
    if not prefix:
        raise ValueError("job_id or id_prefix is required")
    return post("/jobs/resolve_prefix", {"id_prefix": prefix})["job_id"]


def get(path: str) -> Dict:
    with urllib.request.urlopen(SERVICE_URL + path, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post(path: str, payload: Dict) -> Dict:
    data = json.dumps({key: value for key, value in payload.items() if value is not None}).encode("utf-8")
    request = urllib.request.Request(SERVICE_URL + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def result(rpc_id, payload: Dict) -> Dict:
    response = {"jsonrpc": "2.0", "result": payload}
    if rpc_id is not None:
        response["id"] = rpc_id
    return response


def error(rpc_id, code: int, message: str) -> Dict:
    response = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if rpc_id is not None:
        response["id"] = rpc_id
    return response


if __name__ == "__main__":
    main()
