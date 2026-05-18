import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import plugin, { collectWithSoftTimeout, resetCollectionForTests, resolveJobId, resolveLeadId } from "../index.js";

const expectedToolNames = [
  "jobhunter_get_more_jobs",
  "jobhunter_collect_all_sources",
  "jobhunter_rescore_recent_jobs",
  "jobhunter_usage",
  "jobhunter_show_profile",
  "jobhunter_history",
  "jobhunter_propose_actions",
  "jobhunter_apply_action",
  "jobhunter_revert_action",
  "jobhunter_mark_job",
  "jobhunter_cover_note",
  "jobhunter_query_sql",
  "jobhunter_process_email",
  "leadhunter_get_more_leads",
  "leadhunter_show_icp",
  "leadhunter_save_leads",
  "leadhunter_add_lead_source",
  "leadhunter_mark_lead",
  "leadhunter_draft_pitch",
];

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  resetCollectionForTests();
});

function registeredTools() {
  const tools = [];
  plugin.register({
    registerTool(factory) {
      tools.push(factory());
    },
  });
  return tools;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("registers the expected Jobhunter tools", () => {
  const names = registeredTools().map((tool) => tool.name);
  assert.deepEqual(names, expectedToolNames);
  const manifest = JSON.parse(fs.readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.contracts.tools, expectedToolNames);
});

test("tool descriptions preserve rendering and proposal contracts", () => {
  const tools = new Map(registeredTools().map((tool) => [tool.name, tool]));

  const digestDescription = tools.get("jobhunter_get_more_jobs").description;
  for (const phrase of [
    "queue_is_stale",
    "callback_data",
    "applied:<id_prefix>",
    "presentation.blocks",
    "CALLBACK HANDLING",
    "✓ Applied",
    "Cover note draft",
    "OpenClaw 2026.5.7's callback synthetic-prompt metadata",
    "PERSISTENT TELEGRAM KEYBOARD",
    "My job profile",
    "My ICP profile",
  ]) {
    assert.match(digestDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const leadDescription = tools.get("leadhunter_get_more_leads").description;
  for (const phrase of [
    "lead_reached:<id_prefix>",
    "lead_irrelevant:<id_prefix>",
    "lead_snooze:<id_prefix>",
    "lead_pitch:<id_prefix>",
    "CALLBACK HANDLING",
    "Reached out",
    "Pitch draft",
    "Snoozed leads automatically reappear",
    "Never send outreach automatically",
    "PERSISTENT TELEGRAM KEYBOARD",
    "My ICP profile",
  ]) {
    assert.match(leadDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const proposalDescription = tools.get("jobhunter_propose_actions").description;
  for (const phrase of ["MANDATORY", "message", "action_id", "firecrawl", "exa"]) {
    assert.match(proposalDescription, new RegExp(phrase));
  }

  // Regression guards for per-kind payload examples.
  // Adding/changing these means the agent has to guess the payload shape
  // again, which is what motivated this section. Update both the
  // description AND this assertion list when an action kind's contract changes.
  const perKindMarkers = [
    "bulk_update_jobs",
    "filter_sql",
    "archived",
    "rejected",
    "rescore_jobs",
    "window_hours",
    "scoring_rule_proposal",
    "ruleset",
    "human_followup",
    "suggested_approach",
    "directive_edit",
    "profile_edit",
    "new_about_me",
    "icp_edit",
    "new_icp",
    "icp.local.md",
  ];
  for (const phrase of perKindMarkers) {
    assert.match(
      proposalDescription,
      new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      `propose_actions description must include "${phrase}"`,
    );
  }

  const processEmailDescription = tools.get("jobhunter_process_email").description;
  for (const phrase of ["Gmail Pub/Sub", "email parser", "scores"]) {
    assert.match(processEmailDescription, new RegExp(phrase));
  }

  const showProfileDescription = tools.get("jobhunter_show_profile").description;
  for (const phrase of ["input/profile.local.md", "My job profile", "# About me", "# Directives"]) {
    assert.match(showProfileDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const showIcpDescription = tools.get("leadhunter_show_icp").description;
  for (const phrase of ["input/icp.local.md", "My ICP profile", "PERSISTENT TELEGRAM KEYBOARD"]) {
    assert.match(showIcpDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  // Old leadhunter callback_data scheme (lead_shortlist/lead_reject/lead_pitch) was replaced
  // with the symmetric job-style scheme (reached/skip/later/pitch) per UX feedback — see
  // the `leadhunter_get_more_leads` description and the regression guards at line ~78 above.

  const saveLeadsDescription = tools.get("leadhunter_save_leads").description;
  for (const phrase of ["explicit user approval", "public url", "Do not store guessed personal emails"]) {
    assert.match(saveLeadsDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
});

test("resolveJobId returns an explicit job_id without fetching", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({ job_id: "unused" });
  };

  assert.equal(await resolveJobId({ job_id: "  abc123  " }), "abc123");
  assert.equal(called, false);
});

test("resolveJobId resolves a 12-character id_prefix through the service", async () => {
  let request = null;
  globalThis.fetch = async (url, options) => {
    request = { url: String(url), body: JSON.parse(options.body) };
    return jsonResponse({ job_id: "resolved-job-id" });
  };

  assert.equal(await resolveJobId({ id_prefix: "abcdef123456" }), "resolved-job-id");
  assert.equal(request.url, "http://jobhunter-service:8765/jobs/resolve_prefix");
  assert.deepEqual(request.body, { id_prefix: "abcdef123456" });
});

test("resolveJobId rejects missing identifiers", async () => {
  await assert.rejects(() => resolveJobId({}), /job_id or id_prefix is required/);
});

test("resolveLeadId resolves id_prefix through the service", async () => {
  let request = null;
  globalThis.fetch = async (url, options) => {
    request = { url: String(url), body: JSON.parse(options.body) };
    return jsonResponse({ lead_id: "resolved-lead-id" });
  };

  assert.equal(await resolveLeadId({ id_prefix: "abcdef123456" }), "resolved-lead-id");
  assert.equal(request.url, "http://jobhunter-service:8765/leads/resolve_prefix");
  assert.deepEqual(request.body, { id_prefix: "abcdef123456" });
});

test("collectWithSoftTimeout returns running while collection is still pending", async () => {
  let fetchSettled = false;
  globalThis.fetch = async () =>
    new Promise((resolve) => {
      const timer = setTimeout(() => {
        fetchSettled = true;
        resolve(jsonResponse({ status: "completed", completed: true }));
      }, 40000);
      timer.unref();
    });

  const startedAt = Date.now();
  const result = await collectWithSoftTimeout(5);

  assert.equal(result.status, "running");
  assert.equal(result.completed, false);
  assert.match(result.message, /still running/i);
  assert.equal(fetchSettled, false);
  assert.ok(Date.now() - startedAt < 1000);
});

test("tool execute maps HTTP JSON errors into Error messages", async () => {
  globalThis.fetch = async () => jsonResponse({ error: "service down" }, 503);
  const usage = registeredTools().find((tool) => tool.name === "jobhunter_usage");

  await assert.rejects(() => usage.execute("tool-call-id", {}), /service down/);
});

test("profile and ICP tools call the read-only service endpoints", async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return jsonResponse({ ok: true, text: "hello" });
  };
  const tools = new Map(registeredTools().map((tool) => [tool.name, tool]));

  await tools.get("jobhunter_show_profile").execute("tool-call-id", {});
  await tools.get("leadhunter_show_icp").execute("tool-call-id", {});

  assert.deepEqual(calls, [
    "http://jobhunter-service:8765/profile/show",
    "http://jobhunter-service:8765/leads/icp/show",
  ]);
});
