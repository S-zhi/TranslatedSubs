import { test } from "node:test";
import assert from "node:assert/strict";

const elements = new Map();
const connection = { open: false };
const tokenInput = { focused: false, focus() { this.focused = true; } };
const list = { children: [], replaceChildren(...nodes) { this.children = nodes; } };

globalThis.window = {
  APP_CONFIG: { USE_MOCK: false, API_BASE_URL: "http://localhost:8000" },
  localStorage: { getItem: () => null },
};
globalThis.document = {
  createElement(tag) {
    const element = {
      tag, innerHTML: "", textContent: "", listeners: {}, style: {},
      addEventListener(type, callback) { this.listeners[type] = callback; },
      append(...nodes) { this.children = nodes; },
    };
    return element;
  },
  createDocumentFragment() { return { append() {} }; },
  querySelector(selector) {
    if (selector === "#taskList") return list;
    if (selector === "#apiConnection") return connection;
    if (selector === "#apiConnectionToken") return tokenInput;
    return elements.get(selector) || null;
  },
  dispatchEvent() {},
};
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };

const { initQueue } = await import("../js/ui-queue.js");
const { loadTasks, state } = await import("../js/store.js");

initQueue({ listSelector: "#taskList" });

function queueError() {
  return list.children[0];
}

test("queue explains a 401 and opens API Token settings", async () => {
  globalThis.fetch = async () => ({ ok: false, status: 401, text: async () => JSON.stringify({ detail: "未提供有效的 API Token" }) });
  await loadTasks();
  assert.equal(state.loadErrorStatus, 401);
  assert.match(queueError().innerHTML, /需要 API Token/);
  assert.doesNotMatch(queueError().innerHTML, /无法连接后端/);
  queueError().children[0].listeners.click();
  assert.equal(state.view, "other-settings");
  assert.equal(connection.open, true);
  assert.equal(tokenInput.focused, true);
});

test("queue retains connection guidance for a network failure", async () => {
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await loadTasks();
  assert.equal(state.loadErrorStatus, null);
  assert.match(queueError().innerHTML, /无法连接后端/);
});
