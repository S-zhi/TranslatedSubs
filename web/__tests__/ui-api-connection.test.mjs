import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = { APP_CONFIG: { USE_MOCK: false, API_BASE_URL: "http://localhost:8000" } };
const handlers = {};
globalThis.document = { addEventListener(name, handler) { handlers[name] = handler; } };
const { initApiConnection } = await import("../js/ui-api-connection.js");
const { getApiToken, setApiToken } = await import("../js/api-auth.js");

function fixture(base) {
  const nodes = new Map();
  for (const key of ["form", "#apiConnectionToken", "#apiConnectionFeedback", "#apiConnectionBase", "#apiConnectionClear"]) {
    nodes.set(key, { value: "", handlers: {}, addEventListener(name, handler) { this.handlers[name] = handler; }, focus() { this.focused = true; } });
  }
  const root = { querySelector: (key) => nodes.get(key) };
  let reloads = 0;
  let shown = 0;
  initApiConnection({ root, base, reload: () => reloads++, showSettings: () => shown++ });
  return { nodes, root, reloads: () => reloads, shown: () => shown };
}

test("connection form restores tab token, saves and reloads all connections, and clears", () => {
  const base = "http://form.example";
  setApiToken(base, "old-token");
  const f = fixture(base);
  const input = f.nodes.get("#apiConnectionToken");
  assert.equal(input.value, "old-token");
  assert.equal(f.nodes.get("#apiConnectionBase").textContent, base);
  input.value = "new-token";
  let prevented = false;
  f.nodes.get("form").handlers.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(getApiToken(base), "new-token");
  assert.equal(f.reloads(), 1);
  f.nodes.get("#apiConnectionClear").handlers.click();
  assert.equal(getApiToken(base), "");
  assert.equal(input.value, "");
  assert.equal(f.reloads(), 2);
});

test("authentication failure opens settings once without exposing token in feedback", () => {
  const f = fixture("http://unauthorized.example");
  handlers["subtrans:api-auth-required"]();
  handlers["subtrans:api-auth-required"]();
  assert.equal(f.root.open, true);
  assert.equal(f.nodes.get("#apiConnectionToken").focused, true);
  assert.match(f.nodes.get("#apiConnectionFeedback").textContent, /API Token/);
  assert.equal(f.shown(), 1);
});
