import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = { APP_CONFIG: { USE_MOCK: false, API_BASE_URL: "http://localhost:8000" } };
const { Api } = await import("../js/api.js");
const { setApiToken } = await import("../js/api-auth.js");
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push({ url, options });
  return { ok: true, json: async () => ({ revision: 9, instance: { id: "created" } }) };
};

test("shared REST wrapper authenticates reads, CAS writes and multipart uploads", async () => {
  setApiToken(Api.base, "tab-token");
  await Api.listPluginInstances();
  await Api.createPluginInstance({ name: "test" }, 8);
  await Api.createUploadTask({ file: new File(["media"], "video.mp4", { type: "video/mp4" }), pluginInstances: {} });
  assert.equal(calls.length, 3);
  assert.equal(calls.every(({ options }) => options.headers.authorization === "Bearer tab-token"), true);
  assert.equal(calls[1].options.headers["if-match"], "8");
  assert.equal(calls[1].options.headers["content-type"], "application/json");
  assert.equal(calls[2].options.headers["content-type"], undefined);
  setApiToken(Api.base, "");
});

test("all artifact and file URLs authenticate without losing path queries", () => {
  setApiToken(Api.base, "token&?/#");
  for (const kind of ["source", "subtitle", "dubbed", "video"]) {
    const url = new URL(Api.downloadUrl("task_1", kind));
    assert.equal(url.searchParams.get("token"), "token&?/#");
  }
  const url = new URL(Api.taskFileUrl("task_1", "nested/sub title.srt"));
  assert.equal(url.searchParams.get("token"), "token&?/#");
  assert.equal(url.searchParams.get("path"), "nested/sub title.srt");
  setApiToken(Api.base, "");
});

test("SSE reconnect reads the current credential and disposal cancels further attempts", () => {
  const sources = [];
  const savedSource = globalThis.EventSource;
  const savedTimer = globalThis.setTimeout;
  const savedClear = globalThis.clearTimeout;
  const timers = new Map();
  globalThis.EventSource = class {
    constructor(url) { this.url = url; sources.push(this); }
    addEventListener() {}
    close() { this.closed = true; }
  };
  globalThis.setTimeout = (fn) => { const id = timers.size + 1; timers.set(id, fn); return id; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  try {
    setApiToken(Api.base, "first");
    const stop = Api.subscribeProgress("task_1", () => {});
    assert.equal(new URL(sources[0].url).searchParams.get("token"), "first");
    sources[0].onerror();
    assert.equal(sources[0].closed, true);
    setApiToken(Api.base, "rotated");
    const callback = timers.get(1);
    timers.delete(1);
    callback();
    assert.equal(new URL(sources[1].url).searchParams.get("token"), "rotated");
    sources[1].onerror();
    stop();
    assert.equal(sources[1].closed, true);
    assert.equal(timers.size, 0);
  } finally {
    globalThis.EventSource = savedSource;
    globalThis.setTimeout = savedTimer;
    globalThis.clearTimeout = savedClear;
    setApiToken(Api.base, "");
  }
});

test("401 notifies connection UI and retains the backend status and detail", async () => {
  const events = [];
  globalThis.document = { dispatchEvent: (event) => events.push(event.type) };
  const savedEvent = globalThis.CustomEvent;
  globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
  globalThis.fetch = async () => ({ ok: false, status: 401, text: async () => JSON.stringify({ detail: "未提供有效的 API Token" }) });
  try {
    await assert.rejects(Api.listPluginInstances(), (error) => error.status === 401 && /API Token/.test(error.message));
    assert.deepEqual(events, ["subtrans:api-auth-required"]);
  } finally {
    delete globalThis.document;
    globalThis.CustomEvent = savedEvent;
  }
});
