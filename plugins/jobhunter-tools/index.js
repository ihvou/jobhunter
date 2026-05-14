import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const SERVICE_URL = (process.env.JOBHUNTER_SERVICE_URL || "http://jobhunter-service:8765").replace(/\/+$/, "");
const COLLECT_SOFT_TIMEOUT_MS = 28000;

let activeCollection = null;

function jsonResult(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: payload,
  };
}

function schema(properties, required = []) {
  return {
    type: "object",
    properties,
    required,
    additionalProperties: false,
  };
}

async function post(path, payload = {}) {
  const response = await fetch(`${SERVICE_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { raw: text };
  }
  if (!response.ok) {
    throw new Error(parsed?.error || `Jobhunter service returned HTTP ${response.status}`);
  }
  return parsed;
}

async function get(path) {
  const response = await fetch(`${SERVICE_URL}${path}`);
  const text = await response.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { raw: text };
  }
  if (!response.ok) {
    throw new Error(parsed?.error || `Jobhunter service returned HTTP ${response.status}`);
  }
  return parsed;
}

async function resolveJobId(params) {
  if (typeof params.job_id === "string" && params.job_id.trim()) {
    return params.job_id.trim();
  }
  if (typeof params.id_prefix === "string" && params.id_prefix.trim()) {
    const resolved = await post("/jobs/resolve_prefix", { id_prefix: params.id_prefix.trim() });
    if (typeof resolved.job_id === "string" && resolved.job_id) {
      return resolved.job_id;
    }
  }
  throw new Error("job_id or id_prefix is required");
}

function runCollection() {
  if (!activeCollection) {
    activeCollection = post("/collect", {})
      .catch((error) => ({ status: "error", completed: false, error: error?.message || String(error) }))
      .finally(() => {
        activeCollection = null;
      });
  }
  return activeCollection;
}

async function collectWithSoftTimeout() {
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      resolve({
        status: "running",
        completed: false,
        message: "Collection is still running in the background. Call jobhunter_get_more_jobs again shortly.",
      });
    }, COLLECT_SOFT_TIMEOUT_MS);
  });
  const result = await Promise.race([runCollection(), timeout]);
  if (timer) {
    clearTimeout(timer);
  }
  return result;
}

function register(api, tool) {
  api.registerTool(() => tool, { name: tool.name });
}

const intSchema = (minimum, maximum) => ({ type: "integer", minimum, maximum });

export default definePluginEntry({
  id: "jobhunter-tools",
  name: "Jobhunter Tools",
  description: "OpenClaw dynamic tool bridge for the local Jobhunter service.",
  register(api) {
    register(api, {
      name: "jobhunter_get_more_jobs",
      label: "Jobhunter Get More Jobs",
      description:
        "Return ranked job matches from Jobhunter. For Telegram digest requests, if queue_is_stale is true or queue_freshness_hours >= 6, call jobhunter_collect_all_sources first, then call this again. Render each shown job with the OpenClaw message tool using presentation.blocks[].buttons for Applied, Irrelevant, Snooze, and Cover. Use mark_sent=true only for rows actually shown. Do not use bash or shell for Jobhunter digest requests.",
      parameters: schema({
        limit: intSchema(1, 25),
        mark_sent: { type: "boolean" },
      }),
      execute: async (_toolCallId, params) => jsonResult(await post("/digest", params)),
    });

    register(api, {
      name: "jobhunter_collect_all_sources",
      label: "Jobhunter Collect All Sources",
      description:
        "Collect and index jobs from all enabled Jobhunter sources. The real collection may take 45-120 seconds; this tool waits up to about 28 seconds and then returns status=running while collection continues in the background. If it returns running, call jobhunter_get_more_jobs again shortly.",
      parameters: schema({}),
      execute: async () => jsonResult(await collectWithSoftTimeout()),
    });

    register(api, {
      name: "jobhunter_usage",
      label: "Jobhunter Usage",
      description: "Return local spend, quota, and recent activity counters.",
      parameters: schema({}),
      execute: async () => jsonResult(await get("/usage")),
    });

    register(api, {
      name: "jobhunter_history",
      label: "Jobhunter History",
      description: "Return recent approved/applied agent action audit rows.",
      parameters: schema({
        limit: intSchema(1, 50),
      }),
      execute: async (_toolCallId, params) => {
        const limit = Number.isInteger(params.limit) ? params.limit : 10;
        return jsonResult(await get(`/history?limit=${encodeURIComponent(String(limit))}`));
      },
    });

    register(api, {
      name: "jobhunter_propose_actions",
      label: "Jobhunter Propose Actions",
      description:
        "Store bounded Jobhunter actions for user approval. For Update sources, propose kind=sources_proposal. For Tune scoring, propose kind=scoring_rule_proposal. Do not call jobhunter_apply_action until explicit user approval.",
      parameters: schema(
        {
          session_id: { type: "string" },
          user_intent: { type: "string" },
          actions: { type: "array" },
        },
        ["actions"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/action/propose", params)),
    });

    register(api, {
      name: "jobhunter_apply_action",
      label: "Jobhunter Apply Action",
      description: "Apply one previously proposed Jobhunter action after explicit user approval.",
      parameters: schema(
        {
          action_id: { type: "integer" },
          confirm: { type: "boolean" },
        },
        ["action_id"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/action/apply", params)),
    });

    register(api, {
      name: "jobhunter_revert_action",
      label: "Jobhunter Revert Action",
      description: "Revert a reversible Jobhunter action by audit id.",
      parameters: schema(
        {
          action_id: { type: "integer" },
        },
        ["action_id"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/action/revert", params)),
    });

    register(api, {
      name: "jobhunter_mark_job",
      label: "Jobhunter Mark Job",
      description:
        "Mark a job as irrelevant, applied, or snoozed. Use job_id or the 12-character id_prefix from inline callback data.",
      parameters: schema({
        job_id: { type: "string" },
        id_prefix: { type: "string", pattern: "^[0-9a-f]{12}$" },
        action: { type: "string", enum: ["irrelevant", "applied", "snooze", "snoozed"] },
        status: { type: "string", enum: ["irrelevant", "applied", "snoozed"] },
        details: { type: "string" },
        snooze_days: intSchema(1, 7),
      }),
      execute: async (_toolCallId, params) => {
        const job_id = await resolveJobId(params);
        const action = params.action || params.status;
        if (action === "applied") {
          return jsonResult(await post("/applied", { job_id, details: params.details || "" }));
        }
        if (action === "snooze" || action === "snoozed") {
          return jsonResult(await post("/snooze", { job_id, snooze_days: params.snooze_days }));
        }
        return jsonResult(await post("/irrelevant", { job_id, details: params.details || "" }));
      },
    });

    register(api, {
      name: "jobhunter_cover_note",
      label: "Jobhunter Cover Note",
      description: "Draft a cover note for one job. Use job_id or the 12-character id_prefix from an inline button.",
      parameters: schema({
        job_id: { type: "string" },
        id_prefix: { type: "string", pattern: "^[0-9a-f]{12}$" },
        override_budget: { type: "boolean" },
      }),
      execute: async (_toolCallId, params) => {
        const job_id = await resolveJobId(params);
        return jsonResult(await post("/cover-note", { job_id, override_budget: params.override_budget === true }));
      },
    });

    register(api, {
      name: "jobhunter_query_sql",
      label: "Jobhunter Query SQL",
      description: "Run a SELECT-only query against Jobhunter SQLite for investigation.",
      parameters: schema(
        {
          sql: { type: "string" },
          params: { type: "array" },
          limit: intSchema(1, 100),
        },
        ["sql"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/query-sql", params)),
    });
  },
});
