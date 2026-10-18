import { test } from "node:test";
import assert from "node:assert/strict";
import { apiAuthHeaders, authenticatedApiUrl, getApiToken, setApiToken } from "../js/api-auth.js";

const stored = new Map();
globalThis.window = {
  location: { href: "http://localhost:8000/tasks" },
  sessionStorage: {
    getItem: (key) => stored.get(key) || null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: (key) => stored.delete(key),
  },
};

test("API credentials are scoped to backend and retained only in session storage", () => {
  const base = "http://auth.example:8000";
  setApiToken(base, " test-token ");
  assert.equal(getApiToken(`${base}/`), "test-token");
  assert.equal(getApiToken("http://other.example:8000"), "");
  assert.equal(stored.get(`subtrans:api-token:${base}`), "test-token");
  setApiToken(base, "");
  assert.equal(getApiToken(base), "");
  assert.equal(stored.has(`subtrans:api-token:${base}`), false);
});

test("REST authentication preserves JSON and CAS headers without forcing multipart content type", () => {
  const base = "http://headers.example";
  setApiToken(base, "secret");
  const headers = apiAuthHeaders(base, { "Content-Type": "application/json", "If-Match": "8" });
  assert.equal(headers.authorization, "Bearer secret");
  assert.equal(headers["content-type"], "application/json");
  assert.equal(headers["if-match"], "8");
  assert.deepEqual(apiAuthHeaders(base), { authorization: "Bearer secret" });
  assert.equal(apiAuthHeaders("http://no-auth.example"), undefined);
});

test("SSE and media URLs preserve existing queries and encode reserved token characters", () => {
  const base = "http://urls.example";
  setApiToken(base, "value&with?reserved/#");
  const result = new URL(authenticatedApiUrl(base, "/api/tasks/id/video?preview=true"));
  assert.equal(result.searchParams.get("preview"), "true");
  assert.equal(result.searchParams.get("token"), "value&with?reserved/#");
  assert.equal(authenticatedApiUrl("http://public.example", "/api/tasks"), "http://public.example/api/tasks");
});

test("blocked browser storage keeps in-memory credentials usable and clearable", () => {
  const storage = window.sessionStorage;
  Object.defineProperty(window, "sessionStorage", { configurable: true, get() { throw new Error("blocked"); } });
  assert.equal(getApiToken("http://blocked.example"), "");
  setApiToken("http://blocked.example", "memory-only");
  assert.equal(getApiToken("http://blocked.example"), "memory-only");
  setApiToken("http://blocked.example", "");
  assert.equal(getApiToken("http://blocked.example"), "");
  Object.defineProperty(window, "sessionStorage", { configurable: true, value: storage });
});
