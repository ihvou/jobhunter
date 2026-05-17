function definePluginEntry(entry) {
  return {
    id: entry.id,
    name: entry.name,
    description: entry.description,
    configSchema: { type: "object", additionalProperties: false, properties: {} },
    register: entry.register,
  };
}

const SERVICE_URL = (process.env.JOBHUNTER_SERVICE_URL || "http://jobhunter-service:8765").replace(/\/+$/, "");
export const COLLECT_SOFT_TIMEOUT_MS = 28000;

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

export async function resolveJobId(params) {
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

export async function resolveLeadId(params) {
  if (typeof params.lead_id === "string" && params.lead_id.trim()) {
    return params.lead_id.trim();
  }
  if (typeof params.id_prefix === "string" && params.id_prefix.trim()) {
    const resolved = await post("/leads/resolve_prefix", { id_prefix: params.id_prefix.trim() });
    if (typeof resolved.lead_id === "string" && resolved.lead_id) {
      return resolved.lead_id;
    }
  }
  throw new Error("lead_id or id_prefix is required");
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

export function resetCollectionForTests() {
  activeCollection = null;
}

export async function collectWithSoftTimeout(timeoutMs = COLLECT_SOFT_TIMEOUT_MS) {
  let timer = null;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      resolve({
        status: "running",
        completed: false,
        message: "Collection is still running in the background. Call jobhunter_get_more_jobs again shortly.",
      });
    }, timeoutMs);
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
        "Return ranked job matches from Jobhunter. " +
        "STALENESS RULE: response includes queue_freshness_hours, queue_last_collected, queue_is_stale. " +
        "If queue_is_stale is true OR queue_freshness_hours >= 6, you MUST first call jobhunter_collect_all_sources, " +
        "then call this tool AGAIN. Do not show a stale digest. Tell the user briefly: " +
        "\"Collecting fresh jobs, back in ~1 min.\" " +
        "RENDERING (Telegram digest requests only): for EACH job in jobs[], emit one `message` call using presentation.blocks with " +
        "{action: \"send\", target: <chat_id from conversation metadata, e.g. \"telegram:855127987\">, " +
        "message: <job text>, presentation: {blocks: [{type: \"buttons\", buttons: [" +
        "[{text: \"Applied\", callback_data: \"applied:<id_prefix>\", style: \"success\"}, " +
        "{text: \"Irrelevant\", callback_data: \"irrelevant:<id_prefix>\", style: \"danger\"}], " +
        "[{text: \"Snooze\", callback_data: \"snooze:<id_prefix>\"}, " +
        "{text: \"Cover\", callback_data: \"cover:<id_prefix>\", style: \"primary\"}]]}]}}. " +
        "<id_prefix> is the first 12 lowercase hex characters of the job's id. " +
        "Use mark_sent=true only for rows actually shown. " +
        "For read-only diagnostics/analysis/source-or-scoring work, call with mark_sent=false and do NOT emit messages. " +
        "Never use bash or shell for Jobhunter digest requests. " +
        "\n\nCALLBACK HANDLING (when a synthetic user message arrives matching `applied:<12hex>`, " +
        "`irrelevant:<12hex>`, `snooze:<12hex>`, or `cover:<12hex>`): you MUST edit the original job message in-place " +
        "rather than sending a new bare confirmation. The synthetic user message includes a message_id in its " +
        "conversation metadata — that is the digest message that had the buttons. Flow: " +
        "(a) For applied/irrelevant/snooze: call `jobhunter_mark_job` with the matching status, then emit " +
        "`message({action: \"edit\", messageId: <metadata message_id>, target: <chat_id>, " +
        "message: \"~~<original job text>~~\\n\\n✓ <Status> at <ISO date>\", presentation: {blocks: []}})` to " +
        "strike through the job and drop the buttons. Status emoji: ✓ Applied, ✗ Irrelevant, 💤 Snoozed 1d. " +
        "(b) For cover: call `jobhunter_cover_note`, then emit " +
        "`message({action: \"edit\", messageId: <metadata message_id>, target: <chat_id>, " +
        "message: \"<original job text>\\n\\n---\\n**Cover note draft:**\\n<draft text>\", " +
        "presentation: {blocks: [{type: \"buttons\", buttons: [[{text: \"Applied\", callback_data: \"applied:<id_prefix>\", style: \"success\"}, {text: \"Irrelevant\", callback_data: \"irrelevant:<id_prefix>\", style: \"danger\"}]]}]}})` " +
        "to APPEND the cover note text to the original message and keep Applied/Irrelevant buttons so the user can still mark the job. " +
        "Do not send a second message for the cover note — Telegram clutter. Edit in place.",
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
      name: "jobhunter_rescore_recent_jobs",
      label: "Jobhunter Rescore Recent Jobs",
      description:
        "Rescore recent indexed jobs after profile/directive/scoring feedback changes. Intended for scheduled maintenance or explicit user requests. This uses cached job rows and bounded L2 rules; it does not collect new sources.",
      parameters: schema({
        limit: intSchema(1, 1000),
      }),
      execute: async (_toolCallId, params) => jsonResult(await post("/rescore", params)),
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
        "Store bounded Jobhunter actions for user approval. " +
        "`actions` MUST be an array of OBJECTS (not strings, not JSON-stringified). Each object MUST have: " +
        "`kind` (one of: sources_proposal, scoring_rule_proposal, profile_edit, icp_edit, directive_edit, " +
        "rescore_jobs, bulk_update_jobs, human_followup), optional `summary` (≤300 chars human-readable), and `payload` " +
        "(kind-specific object — see below). The server silently drops any element that is not a dict with a " +
        "valid `kind`, so getting the shape right matters. " +
        "\n\nEXAMPLE for adding a source (sources_proposal):\n" +
        "{\n" +
        "  \"kind\": \"sources_proposal\",\n" +
        "  \"summary\": \"Add DOU Product Manager as a community test source\",\n" +
        "  \"payload\": {\n" +
        "    \"operations\": [{\n" +
        "      \"op\": \"add\",\n" +
        "      \"source\": {\n" +
        "        \"id\": \"dou-product-manager\",\n" +
        "        \"name\": \"DOU Product Manager Jobs\",\n" +
        "        \"type\": \"community\",\n" +
        "        \"url\": \"https://jobs.dou.ua/vacancies/?category=Product%20Manager&from=maybe\",\n" +
        "        \"status\": \"test\"\n" +
        "      }\n" +
        "    }]\n" +
        "  }\n" +
        "}\n" +
        "`payload.operations[].op` is one of: add, modify, disable. " +
        "`source.type` is one of: rss, json_api, community, greenhouse, ashby, lever, workable, imap. " +
        "`source.status` is one of: active, test, disabled. " +
        "\n\nOTHER KIND PAYLOADS (use these exact key names — guessing wastes a roundtrip):\n" +
        "- `bulk_update_jobs.payload`: required `filter_sql` (SELECT-only query that returns rows to update) and " +
        "`new_status` which MUST be \"archived\" or \"rejected\" (NOT \"irrelevant\"). Example payload: " +
        "`{filter_sql: \"SELECT id FROM jobs WHERE source_id='email-job-alerts' AND title='Read more'\", new_status: \"archived\"}`.\n" +
        "- `rescore_jobs.payload`: no required keys. Optional `window_hours` (int 1..168, default 24; >48 needs " +
        "user confirm=true) and `source_ids` (array of source ids, max 20). Example: " +
        "`{window_hours: 72, source_ids: [\"email-job-alerts\"]}`.\n" +
        "- `scoring_rule_proposal.payload`: required `ruleset` (full new ruleset object). The agent should " +
        "fetch the current ruleset via jobhunter_query_sql or jobhunter_get_more_jobs metadata first, copy " +
        "the structure, mutate, and submit the entire object — not a diff. Server replaces atomically.\n" +
        "- `human_followup.payload`: required `title` (≤200 chars). Optional `summary`, `suggested_approach`, " +
        "`urgency` (\"low\"|\"medium\"|\"high\"). Do NOT include `evidence`, `details`, `notes`, or other keys — " +
        "server rejects them. Example: " +
        "`{title: \"Tighten LinkedIn email parser\", summary: \"...\", suggested_approach: \"...\", urgency: \"high\"}`. " +
        "Use this kind for multi-file code/parser refactors that need a dedicated Codex session (more time, " +
        "test runs, commit discipline). After filing the candidate AND emitting the message: STOP. Do not start " +
        "modifying code via internal shell in the same turn — large refactors don't fit one Telegram turn and " +
        "you'll likely run out of turn time mid-edit (this has happened). The task candidate IS the handoff to " +
        "the next Codex run, which will pick it up with full context and time.\n" +
        "- `directive_edit.payload`: required `directive` (a one-paragraph instruction appended to the profile " +
        "directives section). Use this only when the user explicitly asks to change their search preferences.\n" +
        "- `profile_edit.payload`: required `new_about_me` (full replacement text, ≤12000 chars). Use sparingly; " +
        "this overwrites the candidate's About section. Confirm intent with the user before proposing.\n" +
        "- `icp_edit.payload`: required `new_icp` (full ICP markdown content, ≤12000 chars). Writes to the " +
        "Leadhunter ICP file at `input/icp.local.md`. Use this when the user says \"save this as my Leadhunter " +
        "ICP\" or \"set my lead ICP to ...\". The file is the source of truth for `leadhunter_*` tools. Existing " +
        "ICP file is backed up timestamped before overwrite. Example: " +
        "`{kind: \"icp_edit\", summary: \"Set Leadhunter ICP to early-stage AI workflow founders\", " +
        "payload: {new_icp: \"# Leadhunter ICP\\n\\nPre-seed/seed B2B workflow SaaS founders, 1-15 people...\"}}`. " +
        "Do NOT use profile_edit to write the ICP — that targets the jobhunter candidate profile, not the lead ICP.\n\n" +
        "For Update sources, propose kind=sources_proposal. For Tune scoring, kind=scoring_rule_proposal. " +
        "Do not call jobhunter_apply_action until explicit user approval. " +
        "SOURCE-FROM-URL FLOW: do NOT pre-validate scraping with web_fetch first. The Python collector has " +
        "different headers, robots-txt handling, and fallback parsers than web_fetch; a 403/404/timeout from " +
        "web_fetch does NOT mean the source is unusable. Propose with status=\"test\" and let the collector try. " +
        "If web_fetch returns 403/404 on a candidate source URL, retry once with firecrawl before defaulting to " +
        "source_type=\"community\" + status=\"test\". If web_fetch fails or you cannot determine source_type, " +
        "default to type=\"community\" and propose anyway. For 'find me sources for X' requests, use exa to " +
        "search first, then propose the top results via jobhunter_propose_actions. " +
        "MANDATORY EMIT: after this tool returns, inspect `actions[]` in the response — each entry has an `id` " +
        "field (the action_id). Your NEXT action MUST be a `message` tool call (action=send, target=<chat_id from " +
        "conversation metadata>) listing every action_id with a one-line human summary so the user can approve. " +
        "Send the approval prompt as **plain text**, not as inline buttons. Approvals are low-frequency, " +
        "high-context decisions; the user may want to qualify or amend the approval (\"approve 39 but switch " +
        "type to rss\", \"approve 39 and run collection now\", \"what's the risk if I leave 39 as test?\"). Forcing " +
        "a binary button cuts off that dialog. Inline buttons are for per-job digest triage only " +
        "(Applied/Irrelevant/Snooze/Cover), NOT for proposal approval. " +
        "If the response has `count: 0` and empty `actions[]`, that means the server REJECTED your input shape — " +
        "still emit a `message` to the user explaining you tried to propose but the shape was wrong, then retry " +
        "with a corrected payload. " +
        "Do NOT continue reasoning, do NOT plan further checks, do NOT end the turn without a `message` call.",
      parameters: schema(
        {
          session_id: { type: "string" },
          user_intent: { type: "string" },
          actions: {
            type: "array",
            items: {
              type: "object",
              required: ["kind"],
              properties: {
                kind: {
                  type: "string",
                  enum: [
                    "sources_proposal",
                    "scoring_rule_proposal",
                    "profile_edit",
                    "directive_edit",
                    "rescore_jobs",
                    "bulk_update_jobs",
                    "human_followup",
                  ],
                },
                summary: { type: "string", maxLength: 300 },
                payload: { type: "object" },
              },
              additionalProperties: false,
            },
            minItems: 1,
          },
        },
        ["actions"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/action/propose", params)),
    });

    register(api, {
      name: "jobhunter_apply_action",
      label: "Jobhunter Apply Action",
      description:
        "Apply one previously proposed Jobhunter action after explicit user approval. " +
        "Always pass confirm=true when the user has approved (e.g. they replied 'approve <id>', 'yes', 'ok'). " +
        "MANDATORY EMIT: after this tool returns — regardless of success or failure — your NEXT action MUST be " +
        "a `message` tool call (action=send, target=<chat_id from conversation metadata>) telling the user the " +
        "outcome in plain text. " +
        "If `ok=true`: confirm what was applied and what happens next (e.g. \"Applied action 39. Source added; " +
        "run 'Get more jobs' to trigger a collection.\"). " +
        "If `ok=false`: tell the user the exact error from `message` field of the response, do NOT fabricate " +
        "optimistic status (\"applying now\", \"once it's in\") if the apply already failed — be honest. If you " +
        "want to retry with a corrected proposal, propose a new action FIRST (via jobhunter_propose_actions), " +
        "then emit a message asking for approval of the new id. " +
        "Do NOT end the turn without a `message` call. Silent endings are a bug.",
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

    register(api, {
      name: "jobhunter_process_email",
      label: "Jobhunter Process Email",
      description:
        "Ingest one job-alert email that arrived through OpenClaw Gmail Pub/Sub, hooks, or an email skill. " +
        "Pass the parsed sender, subject, body, and optional message_id/date. The service runs its existing " +
        "email parser templates, drops known wrapper noise, inserts any jobs, scores them, and may run capped L2 relevance. " +
        "Use this for email-triggered job alerts only; it does not send email or access Gmail directly.",
      parameters: schema(
        {
          source_id: { type: "string" },
          sender: { type: "string" },
          subject: { type: "string" },
          body: { type: "string" },
          message_id: { type: "string" },
          date: { type: "string" },
        },
        ["sender", "subject", "body"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/email/process", params)),
    });

    register(api, {
      name: "leadhunter_get_more_leads",
      label: "Leadhunter Get More Leads",
      description:
        "Return saved lead candidates from the local Jobhunter service. Use this when the user sends /leads, says " +
        "\"get more leads\", asks for a lead digest, or asks to see researched leads. " +
        "If leads[] is empty, do NOT silently return — emit a single `message` explaining the empty state " +
        "(e.g. \"no saved leads yet — first set your Leadhunter ICP via icp_edit, then add a lead_source, then research\"). " +
        "RENDERING (Telegram lead digest requests only): for EACH lead in leads[], emit one `message` call with " +
        "{action: \"send\", target: <chat_id from conversation metadata>, message: <lead text>, " +
        "presentation: {blocks: [{type: \"buttons\", buttons: [" +
        "[{text: \"Reached out\", callback_data: \"reached:<id_prefix>\", style: \"success\"}, " +
        "{text: \"Skip\", callback_data: \"skip:<id_prefix>\", style: \"danger\"}], " +
        "[{text: \"Later\", callback_data: \"later:<id_prefix>\"}, " +
        "{text: \"Pitch\", callback_data: \"pitch:<id_prefix>\", style: \"primary\"}]]}]}}. " +
        "<id_prefix> is the first 12 lowercase hex characters of the lead id. " +
        "Use mark_sent=true only for rows actually shown. " +
        "\n\nCALLBACK HANDLING (when a synthetic user message arrives matching `reached:<12hex>`, " +
        "`skip:<12hex>`, `later:<12hex>`, or `pitch:<12hex>`): edit the original lead message in-place. " +
        "(a) For reached/skip/later: call `leadhunter_mark_lead` with matching status, then emit " +
        "`message({action: \"edit\", messageId: <metadata message_id>, target: <chat_id>, " +
        "message: \"~~<original lead text>~~\\n\\n✓ <Status> at <ISO date>\", presentation: {blocks: []}})`. " +
        "Status emoji: ✓ Reached out, ✗ Skipped, 💤 Later (snoozed 7d). " +
        "(b) For pitch: call `leadhunter_draft_pitch`, then emit " +
        "`message({action: \"edit\", messageId: <metadata message_id>, target: <chat_id>, " +
        "message: \"<original lead text>\\n\\n---\\n**Pitch draft:**\\n<draft text>\", " +
        "presentation: {blocks: [{type: \"buttons\", buttons: [[{text: \"Reached out\", callback_data: \"reached:<id_prefix>\", style: \"success\"}, {text: \"Skip\", callback_data: \"skip:<id_prefix>\", style: \"danger\"}]]}]}})` " +
        "to APPEND the pitch to the original message and keep Reached out/Skip buttons. " +
        "Never send outreach automatically — drafts only.",
      parameters: schema({
        limit: intSchema(1, 25),
        mark_sent: { type: "boolean" },
      }),
      execute: async (_toolCallId, params) => jsonResult(await post("/leads/digest", params)),
    });

    register(api, {
      name: "leadhunter_save_leads",
      label: "Leadhunter Save Leads",
      description:
        "Save researched lead candidates after explicit user approval. Do NOT call this immediately after web_search/firecrawl/exa research unless the user has approved the candidate list. " +
        "Lead objects need a public url plus person_name or company. Optional fields: role, source_name, source_url, contact_surface, evidence[], why_match, confidence 0-100, risk_level low|medium|high, notes. " +
        "Use public professional evidence only. Do not store guessed personal emails, private LinkedIn/cookie data, or hidden contact details.",
      parameters: schema(
        {
          session_id: { type: "string" },
          user_intent: { type: "string" },
          leads: {
            type: "array",
            minItems: 1,
            maxItems: 25,
            items: {
              type: "object",
              properties: {
                person_name: { type: "string" },
                name: { type: "string" },
                company: { type: "string" },
                role: { type: "string" },
                title: { type: "string" },
                url: { type: "string" },
                profile_url: { type: "string" },
                evidence_url: { type: "string" },
                source_name: { type: "string" },
                source_url: { type: "string" },
                contact_surface: { type: "string" },
                evidence: { type: "array", items: { type: "string" } },
                why_match: { type: "string" },
                confidence: intSchema(0, 100),
                risk_level: { type: "string", enum: ["low", "medium", "high"] },
                notes: { type: "string" },
              },
              additionalProperties: true,
            },
          },
        },
        ["leads"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/leads/research", params)),
    });

    register(api, {
      name: "leadhunter_add_lead_source",
      label: "Leadhunter Add Lead Source",
      description:
        "Save one public lead-source candidate after user approval. Good source types: public_directory, company_page, funding_news, conference, community, api, other. " +
        "Use for repeatable public places where future lead research should look. Do not add logged-in LinkedIn, browser-cookie, or scraped private data sources.",
      parameters: schema(
        {
          session_id: { type: "string" },
          user_intent: { type: "string" },
          id: { type: "string" },
          name: { type: "string" },
          type: {
            type: "string",
            enum: ["public_directory", "company_page", "funding_news", "conference", "community", "api", "other"],
          },
          url: { type: "string" },
          status: { type: "string", enum: ["test", "active", "disabled"] },
          risk_level: { type: "string", enum: ["low", "medium", "high"] },
          notes: { type: "string" },
        },
        ["url"],
      ),
      execute: async (_toolCallId, params) => jsonResult(await post("/leads/source/add", params)),
    });

    register(api, {
      name: "leadhunter_mark_lead",
      label: "Leadhunter Mark Lead",
      description:
        "Mark a lead as shortlisted, rejected, pitched, or archived. Use lead_id or the 12-character id_prefix from lead inline callback data. This records status only; it never sends outreach.",
      parameters: schema({
        lead_id: { type: "string" },
        id_prefix: { type: "string", pattern: "^[0-9a-f]{12}$" },
        action: { type: "string", enum: ["shortlisted", "rejected", "pitch", "pitched", "archived"] },
        status: { type: "string", enum: ["shortlisted", "rejected", "pitched", "archived"] },
        details: { type: "string" },
      }),
      execute: async (_toolCallId, params) => {
        const lead_id = await resolveLeadId(params);
        let status = params.status || params.action;
        if (status === "pitch") {
          status = "pitched";
        }
        return jsonResult(await post("/leads/mark", { lead_id, status, details: params.details || "" }));
      },
    });

    register(api, {
      name: "leadhunter_draft_pitch",
      label: "Leadhunter Draft Pitch",
      description:
        "Draft a short copy-paste DM for one lead. Use lead_id or id_prefix from an inline button. This tool only drafts text; it must never send messages, email, or LinkedIn outreach.",
      parameters: schema({
        lead_id: { type: "string" },
        id_prefix: { type: "string", pattern: "^[0-9a-f]{12}$" },
        ask: { type: "string" },
      }),
      execute: async (_toolCallId, params) => {
        const lead_id = await resolveLeadId(params);
        return jsonResult(await post("/leads/pitch", { lead_id, ask: params.ask || "" }));
      },
    });
  },
});
