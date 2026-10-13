import { test } from "node:test";
import assert from "node:assert/strict";

class NodeMock {
  constructor() { this.handlers = {}; this.dataset = {}; this.classList = { add() {}, remove() {} }; this.hidden = false; this.children = []; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelectorAll(selector) { return this.lists?.[selector] || []; }
  setAttribute() {}
  removeAttribute() {}
}

class DocumentMock {
  constructor() { this.nodes = new Map(); this.handlers = {}; this.body = { dataset: { route: "tasks" } }; this.visibilityState = "visible"; }
  querySelector(selector) { return this.nodes.get(selector) || null; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  dispatchEvent(event) { this.handlers[event.type]?.(event); return true; }
}

globalThis.document = new DocumentMock();
globalThis.window = { confirm: () => true };
globalThis.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } };
const timers = new Map();
let timerId = 0;
globalThis.setInterval = (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; };
globalThis.clearInterval = (id) => timers.delete(id);

const { Api } = await import("../js/api.js");
const { initLocalModels } = await import("../js/ui-local-models.js");

test("Whisper view owns one poll timer and stops it when kind or route changes", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  const form = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  const calls = t.mock.method(Api, "listLocalModels", async () => []);
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: "local:tiny" } }));
  initLocalModels();
  document.body.dataset.route = "model-settings";
  document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: "model-settings" } }));
  document.dispatchEvent(new CustomEvent("modelkindchange", { detail: { kind: "whisper" } }));
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].ms, 2500);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.mock.callCount(), 1);
  document.dispatchEvent(new CustomEvent("modelkindchange", { detail: { kind: "llm" } }));
  assert.equal(timers.size, 0);
  document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: "tasks" } }));
  assert.equal(timers.size, 0);
});

test("inventory response started on Whisper becomes stale after leaving the page", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  const form = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  let resolveList;
  t.mock.method(Api, "listLocalModels", () => new Promise((resolve) => { resolveList = resolve; }));
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: "local:tiny" } }));
  const inventoryListener = t.mock.method(document, "dispatchEvent", function dispatch(event) {
    if (event.type === "whisper-models-change") this.handlers[event.type]?.(event);
    return true;
  });
  initLocalModels();
  document.body.dataset.route = "model-settings";
  document.handlers.modelkindchange({ detail: { kind: "whisper" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof resolveList, "function");
  document.handlers.viewchange({ detail: { view: "tasks" } });
  resolveList([{ name: "tiny", status: "READY" }]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(inventoryListener.mock.calls.filter((call) => call.arguments[0].type === "whisper-models-change").length, 0);
  assert.equal(timers.size, 0);
});

test("the current READY inventory card refuses deletion and gives inline feedback", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  const form = new NodeMock();
  const list = new NodeMock();
  const deleteButton = new NodeMock();
  deleteButton.dataset.modelDelete = "tiny";
  const actions = new NodeMock();
  actions.lists = { "[data-model-delete]": [deleteButton], "[data-model-download]": [], "[data-model-check]": [], "[data-model-current]": [] };
  const feedback = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  document.nodes.set("#localModelsList", list);
  document.nodes.set("#localModelsSettings", actions);
  document.nodes.set("#localModelActionFeedback", feedback);
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", label: "Tiny", source: "imported", status: "READY" }]);
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: "local:tiny" } }));
  const remove = t.mock.method(Api, "deleteLocalModel", async () => {});
  initLocalModels();
  document.handlers["model-settings-change"]({ detail: { settings: { whisper: { model: "local:tiny" } } } });
  document.body.dataset.route = "model-settings";
  document.handlers.viewchange({ detail: { view: "model-settings" } });
  await new Promise((resolve) => setImmediate(resolve));
  await deleteButton.handlers.click();
  assert.equal(remove.mock.callCount(), 0);
  assert.equal(feedback.textContent, "当前模型正在使用，请先切换当前模型后再删除。");
  document.handlers.viewchange({ detail: { view: "tasks" } });
});

test("showing the document again restores one timer and combined refresh carries cross-tab current settings", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  const form = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  let currentModel = "local:tiny";
  const eventSpy = t.mock.method(document, "dispatchEvent", function dispatch(event) {
    if (event.type === "model-settings-change" || event.type === "whisper-models-change") this.handlers[event.type]?.(event);
    return true;
  });
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", status: "READY" }]);
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: currentModel } }));
  initLocalModels();
  document.body.dataset.route = "model-settings";
  document.handlers.viewchange({ detail: { view: "model-settings" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(timers.size, 1);
  document.visibilityState = "hidden";
  document.handlers.visibilitychange();
  assert.equal(timers.size, 0);
  document.visibilityState = "visible";
  currentModel = "local:tiny-updated-in-another-tab";
  document.handlers.visibilitychange();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(timers.size, 1);
  const combined = eventSpy.mock.calls.findLast((call) => call.arguments[0].type === "whisper-models-change");
  assert.equal(combined.arguments[0].detail.whisper.model, currentModel);
});

test("concurrent forced refreshes serialize into a single follow-up pass", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  const form = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  document.body.dataset.route = "model-settings";
  let firstResolve;
  let active = 0;
  let maxActive = 0;
  let calls = 0;
  t.mock.method(Api, "listLocalModels", async () => {
    calls += 1;
    active += 1;
    maxActive = Math.max(maxActive, active);
    if (calls === 1) await new Promise((resolve) => { firstResolve = resolve; });
    active -= 1;
    return [];
  });
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: "local:tiny" } }));
  const { refresh } = await import("../js/ui-local-models.js");
  initLocalModels();
  document.handlers.modelkindchange({ detail: { kind: "whisper" } });
  await new Promise((resolve) => setImmediate(resolve));
  const one = refresh({ force: true });
  const two = refresh({ force: true });
  firstResolve();
  await Promise.all([one, two]);
  assert.equal(maxActive, 1);
  assert.equal(calls, 2);
});

test("a mutation invalidates an older in-flight settings response and performs a fresh combined pass", async (t) => {
  globalThis.document = new DocumentMock();
  timers.clear();
  document.body.dataset.route = "model-settings";
  const form = new NodeMock();
  document.nodes.set("#localModelImportForm", form);
  document.nodes.set("#model-panel-whisper", { hidden: false });
  let resolveFirstInventory;
  let currentModel = "local:old";
  let listCalls = 0;
  const events = [];
  document.addEventListener("whisper-models-change", (event) => events.push(event.detail));
  t.mock.method(Api, "listLocalModels", async () => {
    listCalls += 1;
    if (listCalls === 1) await new Promise((resolve) => { resolveFirstInventory = resolve; });
    return [{ name: "new", status: "READY" }];
  });
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { model: currentModel } }));
  initLocalModels();
  document.handlers.viewchange({ detail: { view: "model-settings" } });
  await new Promise((resolve) => setImmediate(resolve));
  currentModel = "local:new";
  document.handlers["model-settings-change"]({ detail: { settings: { whisper: { model: currentModel } }, source: "mutation" } });
  resolveFirstInventory();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(listCalls, 2);
  assert.equal(events.at(-1).whisper.model, "local:new");
  document.handlers.viewchange({ detail: { view: "tasks" } });
});
