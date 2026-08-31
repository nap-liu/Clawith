import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

globalThis.localStorage = {
  getItem() {
    return "test-token";
  },
  removeItem() {},
};

const apiSource = await readFile(new URL("../src/services/api.ts", import.meta.url), "utf8");
const apiJavaScript = ts.transpileModule(apiSource, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const { agentApi } = await import(
  `data:text/javascript;base64,${Buffer.from(apiJavaScript).toString("base64")}`
);

const makeAgent = (index) => ({
  id: `agent-${index}`,
  name: `Agent ${index}`,
  status: "running",
  creator_id: "creator",
  creator_display_name: "Creator",
  creator_username: "creator",
  agent_type: "standard",
  unread_count: 0,
  created_at: "2026-08-31T00:00:00Z",
});

const pagePayload = (page, items, hasMore) => ({
  items,
  total: 501,
  page,
  page_size: 500,
  has_more: hasMore,
  counts: { all: 501, running: 501, idle: 0, stopped: 0 },
});

let requestedPages = [];
globalThis.fetch = async (url) => {
  const page = Number(new URL(url, "http://test").searchParams.get("page"));
  requestedPages.push(page);
  const payload =
    page === 1
      ? pagePayload(1, Array.from({ length: 500 }, (_, index) => makeAgent(index)), true)
      : pagePayload(2, [makeAgent(500)], false);
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const complete = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.deepEqual(requestedPages, [1, 2]);
assert.equal(complete.items.length, 501);
assert.equal(new Set(complete.items.map((item) => item.id)).size, 501);
assert.equal(complete.incomplete, false);

requestedPages = [];
let failSecondPage = true;
globalThis.fetch = async (url) => {
  const page = Number(new URL(url, "http://test").searchParams.get("page"));
  requestedPages.push(page);
  if (page === 2 && failSecondPage) {
    return new Response(JSON.stringify({ detail: "temporary failure" }), {
      status: 503,
      headers: { "content-type": "application/json" },
    });
  }
  const payload =
    page === 1
      ? pagePayload(1, Array.from({ length: 500 }, (_, index) => makeAgent(index)), true)
      : pagePayload(2, [makeAgent(500)], false);
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const partial = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.equal(partial.items.length, 500);
assert.equal(partial.incomplete, true);
assert.match(partial.load_error || "", /temporary failure/);

failSecondPage = false;
const retried = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.equal(retried.items.length, 501);
assert.equal(retried.incomplete, false);

requestedPages = [];
globalThis.fetch = async (url) => {
  const page = Number(new URL(url, "http://test").searchParams.get("page"));
  requestedPages.push(page);
  const payload =
    page === 1
      ? pagePayload(1, Array.from({ length: 500 }, (_, index) => makeAgent(index)), true)
      : pagePayload(2, [makeAgent(499)], false);
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const duplicateBoundary = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.deepEqual(requestedPages, [1, 2, 1, 2]);
assert.equal(duplicateBoundary.items.length, 500);
assert.equal(duplicateBoundary.incomplete, true);
assert.match(duplicateBoundary.load_error || "", /unique item count/);

requestedPages = [];
let totalChangeAttempt = 0;
globalThis.fetch = async (url) => {
  const page = Number(new URL(url, "http://test").searchParams.get("page"));
  requestedPages.push(page);
  if (page === 1) totalChangeAttempt += 1;
  const stableTotal = totalChangeAttempt > 1 ? 502 : 501;
  const payload =
    page === 1
      ? {
          ...pagePayload(1, Array.from({ length: 500 }, (_, index) => makeAgent(index)), true),
          total: stableTotal,
        }
      : {
          ...pagePayload(2, [makeAgent(500), makeAgent(501)], false),
          total: 502,
        };
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const totalChanged = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.deepEqual(requestedPages, [1, 2, 1, 2]);
assert.equal(totalChanged.items.length, 502);
assert.equal(totalChanged.incomplete, false);

globalThis.fetch = async (url) => {
  const page = Number(new URL(url, "http://test").searchParams.get("page"));
  const samePage = Array.from({ length: 500 }, (_, index) => makeAgent(index));
  return new Response(JSON.stringify(pagePayload(page, samePage, true)), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
};

const stalled = await agentApi.exploreAll({ tenantId: "tenant-a" });
assert.equal(stalled.items.length, 500);
assert.equal(stalled.incomplete, true);
assert.match(stalled.load_error || "", /did not make progress/);

console.log("Agent directory pagination tests passed");
