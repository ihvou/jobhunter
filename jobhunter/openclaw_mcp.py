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
        "description": "Return ranked job matches from Jobhunter. Set mark_sent=true only after sending the jobs to the user.",
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
        "description": "Store bounded Jobhunter actions for user approval. This does not apply changes.",
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
        "name": "jobhunter_agent_request",
        "description": "Queue a legacy Jobhunter agent request. Prefer native OpenClaw reasoning unless explicitly testing rollback.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_text": {"type": "string"}},
            "required": ["user_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_agent_poll",
        "description": "Poll completed legacy Jobhunter agent responses. Prefer native OpenClaw reasoning unless explicitly testing rollback.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_mark_job",
        "description": "Mark a job as irrelevant, applied, or snoozed. Never use without explicit user intent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "action": {"type": "string", "enum": ["irrelevant", "applied", "snooze"]},
                "details": {"type": "string"},
            },
            "required": ["job_id", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "jobhunter_cover_note",
        "description": "Draft a cover note for one job.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}, "override_budget": {"type": "boolean"}},
            "required": ["job_id"],
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
        return post("/digest", {"limit": args.get("limit"), "mark_sent": bool(args.get("mark_sent", False))})
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
    if name == "jobhunter_agent_request":
        return post("/agent/request", {"user_text": args.get("user_text")})
    if name == "jobhunter_agent_poll":
        return post("/agent/poll", {"session_id": args.get("session_id") or ""})
    if name == "jobhunter_mark_job":
        action = args.get("action")
        if action == "irrelevant":
            return post("/irrelevant", {"job_id": args.get("job_id"), "details": args.get("details", "")})
        if action == "applied":
            return post("/applied", {"job_id": args.get("job_id"), "details": args.get("details", "")})
        if action == "snooze":
            return post("/snooze", {"job_id": args.get("job_id")})
    if name == "jobhunter_cover_note":
        return post("/cover-note", {"job_id": args.get("job_id"), "override_budget": bool(args.get("override_budget", False))})
    if name == "jobhunter_query_sql":
        return post("/query-sql", {"sql": args.get("sql"), "params": args.get("params") or [], "limit": args.get("limit") or 50})
    raise ValueError("Unknown tool: %s" % name)


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
