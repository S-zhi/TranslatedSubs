import { test } from "node:test";
import assert from "node:assert/strict";

const listeners = new Map();
const stack = ["/settings?tab=plugins"];
let cursor = 0;
const location = {};
function syncLocation(url) { const parsed = new URL(url, "http://localhost"); Object.assign(location, { pathname: parsed.pathname, search: parsed.search }); }
syncLocation(stack[0]);
globalThis.window = {
  location,
  history: {
    pushState(_state, _title, url) { stack.splice(++cursor); stack.push(url); syncLocation(url); },
    replaceState(_state, _title, url) { stack[cursor] = url; syncLocation(url); },
    back() { if (cursor > 0) { cursor -= 1; syncLocation(stack[cursor]); listeners.get("popstate")?.(); } },
    forward() { if (cursor + 1 < stack.length) { cursor += 1; syncLocation(stack[cursor]); listeners.get("popstate")?.(); } },
  },
  addEventListener: (name, handler) => listeners.set(name, handler),
};
globalThis.document = { title: "", body: { dataset: {} }, addEventListener() {}, dispatchEvent() {} };
const store = await import("../js/store.js");
const { initRouter } = await import("../js/router.js");

test("plugin settings deep link survives initialization, tab clicks and back/forward", () => {
  initRouter();
  assert.equal(store.state.view, "other-settings");
  assert.equal(store.state.settingsTab, "plugins");
  assert.equal(location.pathname + location.search, "/settings?tab=plugins");
  store.setSettingsTab("audio");
  assert.equal(location.search, "?tab=audio");
  store.setSettingsTab("engines");
  assert.equal(location.pathname + location.search, "/settings");
  const length = stack.length;
  window.history.back();
  assert.equal(store.state.settingsTab, "audio");
  window.history.back();
  assert.equal(store.state.settingsTab, "plugins");
  window.history.forward();
  assert.equal(store.state.settingsTab, "audio");
  assert.equal(stack.length, length);
  store.setView("tasks");
  store.setSettingsTab("plugins");
  assert.equal(location.pathname, "/tasks");
  store.setView("other-settings");
  assert.equal(location.pathname + location.search, "/settings?tab=plugins");
});
