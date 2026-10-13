import { test } from "node:test";
import assert from "node:assert/strict";

const listeners = new Map();
const stack = ["/model-settings?kind=whisper"];
let cursor = 0;
const location = {};
function syncLocation(url) {
  const parsed = new URL(url, "http://localhost");
  location.pathname = parsed.pathname;
  location.search = parsed.search;
}
syncLocation(stack[0]);
globalThis.window = {
  location,
  history: {
    pushState(_state, _title, url) { stack.splice(++cursor); stack.push(url); syncLocation(url); },
    replaceState(_state, _title, url) { stack[cursor] = url; syncLocation(url); },
    back() { if (cursor > 0) { cursor -= 1; syncLocation(stack[cursor]); listeners.get("popstate")?.(); } },
    forward() { if (cursor + 1 < stack.length) { cursor += 1; syncLocation(stack[cursor]); listeners.get("popstate")?.(); } },
  },
  addEventListener(name, fn) { listeners.set(name, fn); },
};
globalThis.document = {
  title: "", body: { dataset: {} }, handlers: new Map(),
  addEventListener(name, fn) { this.handlers.set(name, fn); },
  dispatchEvent(event) { this.handlers.get(event.type)?.(event); return true; },
};
globalThis.CustomEvent = class CustomEvent { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } };

const store = await import("../js/store.js");
const { initRouter } = await import("../js/router.js");

test("model-settings kind deep links and history stay synchronized", () => {
  initRouter();
  assert.equal(store.state.view, "model-settings");
  assert.equal(store.state.modelKind, "whisper");
  assert.equal(location.pathname + location.search, "/model-settings?kind=whisper");
  const before = stack.length;
  store.setModelKind("tts");
  assert.equal(location.pathname + location.search, "/model-settings?kind=tts");
  assert.equal(stack.length, before + 1);
  store.setModelKind("llm");
  assert.equal(location.pathname + location.search, "/model-settings");
  window.history.back();
  assert.equal(store.state.modelKind, "tts");
  window.history.back();
  assert.equal(store.state.modelKind, "whisper");
  assert.equal(stack.length, before + 2);
  window.history.forward();
  assert.equal(store.state.modelKind, "tts");
});

test("legacy models tab enters Whisper and non-model settings tabs do not steal the route", () => {
  store.setSettingsTab("models");
  assert.equal(store.state.view, "model-settings");
  assert.equal(store.state.modelKind, "whisper");
  store.setSettingsTab("engines");
  assert.equal(store.state.view, "model-settings");
});

test("model-kind route supports llm and tts while other routes remain intact", () => {
  store.setView("model-settings");
  store.setModelKind("llm");
  assert.equal(location.pathname + location.search, "/model-settings");
  store.setModelKind("tts");
  assert.equal(location.pathname + location.search, "/model-settings?kind=tts");
  store.setView("tasks");
  assert.equal(location.pathname, "/tasks");
});
