import { afterEach, test } from "node:test";
import assert from "node:assert/strict";

import plugin, { collectWithSoftTimeout, resetCollectionForTests, resolveJobId } from "../index.js";

const expectedToolNames = [
  "jobhunter_get_more_jobs",
  "jobhunter_collect_all_sources",
  "jobhunter_usage",
  "jobhunter_history",
  "jobhunter_propose_actions",
  "jobhunter_apply_action",
  "jobhunter_revert_action",
  "jobhunter_mark_job",
  "jobhunter_cover_note",
  "jobhunter_query_sql",
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
});

test("tool descriptions preserve rendering and proposal contracts", () => {
  const tools = new Map(registeredTools().map((tool) => [tool.name, tool]));

  const digestDescription = tools.get("jobhunter_get_more_jobs").description;
  for (const phrase of ["queue_is_stale", "callback_data", "applied:<id_prefix>", "presentation.blocks"]) {
    assert.match(digestDescription, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const proposalDescription = tools.get("jobhunter_propose_actions").description;
  for (const phrase of ["MANDATORY", "message", "action_id", "firecrawl", "exa"]) {
    assert.match(proposalDescription, new RegExp(phrase));
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
