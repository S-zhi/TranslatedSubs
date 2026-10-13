import { test } from "node:test";
import assert from "node:assert/strict";

class NodeMock {
  constructor(name = "") {
    this.name = name;
    this.type = "text";
    this.tagName = "INPUT";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
    this.children = [];
    this.controls = [];
    this.handlers = {};
    this.options = [];
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  addEventListener(name, handler) {
    const previous = this.handlers[name];
    this.handlers[name] = previous ? async (...args) => { await previous(...args); return handler(...args); } : handler;
  }
  dispatchEvent(event) { this.handlers[event.type]?.(event); return true; }
  querySelector(selector) { return selector === 'button[type="submit"]' ? this.submitButton : null; }
  querySelectorAll(selector) {
    if (selector === "input, select") return this.controls;
    if (selector === 'input[type="checkbox"]') return this.controls.filter((item) => item.type === "checkbox");
    return [];
  }
  replaceChildren(...children) { this.children = children; }
  append(child) { this.children.push(child); this.options.push(child); }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
  reportValidity() { return true; }
  reset() {}
  close() {}
}

class DocumentMock {
  constructor() { this.nodes = new Map(); this.lists = new Map(); this.handlers = {}; this.visibilityState = "visible"; this.body = { dataset: { route: "model-settings" } }; }
  querySelector(selector) { return this.nodes.get(selector) || null; }
  querySelectorAll(selector) { return this.lists.get(selector) || []; }
  createElement(tag) { const node = new NodeMock(); node.tagName = tag.toUpperCase(); return node; }
  createTextNode(text) { return { textContent: text }; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  dispatchEvent(event) { this.handlers[event.type]?.(event); return true; }
}

class FormDataMock {
  constructor(form) {
    this.values = form.controls
      .filter((control) => control.name && (control.type !== "checkbox" || control.checked))
      .map((control) => [control.name, control.value]);
  }
  entries() { return this.values[Symbol.iterator](); }
}

globalThis.window = {
  APP_CONFIG: { USE_MOCK: false, API_BASE_URL: "http://localhost:8000", API_TIMEOUT_MS: 1000 },
  location: { search: "" },
  localStorage: { getItem() { return null; }, setItem() {} },
};
globalThis.document = new DocumentMock();
globalThis.FormData = FormDataMock;
globalThis.requestAnimationFrame = (callback) => callback();

const { Api } = await import("../js/api.js");
const { setModelKind } = await import("../js/store.js");
Api.listLocalModels = async () => [];
const { initModelSettings, normalizeModelSettings, selectCatalogModel } = await import("../js/ui-model-settings.js");

function control(name, value, type = "text") {
  const node = new NodeMock(name);
  node.type = type;
  node.tagName = type === "select" ? "SELECT" : "INPUT";
  node.value = value;
  node.checked = type === "checkbox" ? Boolean(value) : false;
  return node;
}

function configureForm(kind, controls) {
  const form = new NodeMock();
  form.dataset.modelForm = kind;
  form.controls = controls;
  form.submitButton = new NodeMock();
  document.nodes.set(`[data-model-form="${kind}"]`, form);
  const forms = document.lists.get("[data-model-form]") || [];
  document.lists.set("[data-model-form]", [...forms, form]);
  return form;
}

function baseSettings() {
  return {
    llm: { enabled: true, provider: "deepseek", model: "old-model", models: [
      { id: "llm-one", name: "First", provider: "deepseek", model: "old-model", enabled: true, isCurrent: true },
      { id: "llm-two", name: "Second", provider: "openai", model: "second-model", enabled: true, isCurrent: false },
    ] },
    whisper: { enabled: true, provider: "local", model: "local:small", models: [] },
    tts: { enabled: false, provider: "kokoro", model: "kokoro-v1", voice: "auto", models: [] },
  };
}

function setupUi({ create = false, cards = false, modelId = "", whisper = false } = {}) {
  globalThis.document = new DocumentMock();
  setModelKind("llm", { history: false });
  const root = new NodeMock();
  root.querySelectorAll = (selector) => document.lists.get(selector) || [];
  document.nodes.set("#modelSettings", root);
  const form = configureForm("llm", [
    control("enabled", true, "checkbox"), control("provider", "deepseek", "select"), control("model", "old-model"),
  ]);
  if (whisper) {
    configureForm("whisper", [control("enabled", true, "checkbox"), control("provider", "local", "select"), control("model", "local:tiny", "select"), control("language", "auto", "select"), control("device", "cpu", "select"), control("threads", 4), control("wordTimestamps", true, "checkbox")]);
    document.nodes.set("#whisperCurrentModel", new NodeMock());
    document.nodes.get("#whisperCurrentModel").value = "local:tiny";
  }
  if (cards) {
    const grid = new NodeMock();
    document.nodes.set('[data-model-cards="llm"]', grid);
  }
  if (create) {
    const createForm = new NodeMock();
    createForm.dataset.modelKind = "llm";
    createForm.dataset.modelId = modelId;
    createForm.controls = [control("name", "New"), control("provider", "openai"), control("model", "new-model"), control("enabled", true, "checkbox"), control("apiKey", "")];
    createForm.elements = Object.fromEntries(createForm.controls.map((item) => [item.name, item]));
    document.nodes.set("#modelCreateForm", createForm);
    document.nodes.set("#modelCreateSubmit", new NodeMock());
    document.nodes.set("#modelCreateFeedback", new NodeMock());
    document.nodes.set("#modelCreateDialog", new NodeMock());
  }
  initModelSettings();
  return { form };
}

async function loadSettings() {
  document.handlers.viewchange({ detail: { view: "model-settings" } });
  await new Promise((resolve) => setImmediate(resolve));
}

test("saving one model kind sends only that kind and updates its current card", async (t) => {
  const saved = baseSettings();
  t.mock.method(Api, "getModelSettings", async () => saved);
  const update = t.mock.method(Api, "updateModelSettings", async (payload) => ({ ...saved, ...payload }));
  const { form } = setupUi();
  await loadSettings();
  form.controls.find((item) => item.name === "model").value = "changed-model";

  await form.handlers.submit({ preventDefault() {} });

  assert.deepEqual(Object.keys(update.mock.calls[0].arguments[0]), ["llm"]);
  assert.equal(update.mock.calls[0].arguments[0].llm.models[0].model, "changed-model");
  assert.equal(update.mock.calls[0].arguments[0].llm.models[1].model, "second-model");
});

test("failed catalog selection restores the previous active model in the form", async (t) => {
  const saved = baseSettings();
  t.mock.method(Api, "getModelSettings", async () => saved);
  t.mock.method(Api, "updateModelSettings", async () => { throw new Error("offline"); });
  const { form } = setupUi();
  await loadSettings();

  await selectCatalogModel("llm", "llm-two");

  assert.equal(form.controls.find((item) => item.name === "model").value, "old-model");
});

test("switching from a second card back to the default card preserves the catalog", async (t) => {
  const saved = baseSettings();
  saved.llm.models[0].models = [];
  const update = t.mock.method(Api, "updateModelSettings", async (payload) => {
    saved.llm = payload.llm;
    return { ...saved };
  });
  t.mock.method(Api, "getModelSettings", async () => saved);
  const { form } = setupUi();
  await loadSettings();

  await selectCatalogModel("llm", "llm-two");
  await selectCatalogModel("llm", "llm-one");
  await loadSettings();

  assert.equal(update.mock.calls[1].arguments[0].llm.models.length, 2);
  assert.equal(update.mock.calls[1].arguments[0].llm.models[0].isCurrent, true);
  assert.equal(form.controls.find((item) => item.name === "model").value, "old-model");
});

test("creating a model uses the server's complete card list without appending a duplicate", async (t) => {
  const saved = baseSettings();
  const currentForm = setupUi({ create: true, cards: true });
  t.mock.method(Api, "getModelSettings", async () => saved);
  const cards = [
    ...saved.llm.models.map((model) => ({ ...model })),
    { id: "llm-new", name: "New", provider: "openai", model: "new-model", isCurrent: true },
  ];
  t.mock.method(Api, "createModelSettings", async () => ({ model: cards[2], config: { ...saved.llm, model: "new-model", models: cards } }));
  await loadSettings();

  await document.nodes.get("#modelCreateForm").handlers.submit({
    preventDefault() {}, currentTarget: document.nodes.get("#modelCreateForm"),
  });

  assert.equal(document.nodes.get('[data-model-cards="llm"]').children.length, 3);
  assert.equal(currentForm.form.controls.find((item) => item.name === "model").value, "new-model");
});

test("editing a non-current model updates only its card without sending a top-level key", async (t) => {
  const saved = baseSettings();
  const { form } = setupUi({ create: true, cards: true, modelId: "llm-two" });
  t.mock.method(Api, "getModelSettings", async () => saved);
  const update = t.mock.method(Api, "updateModelSettings", async (payload) => ({
    ...saved,
    llm: { ...saved.llm, ...payload.llm },
  }));
  await loadSettings();
  const editor = document.nodes.get("#modelCreateForm");
  editor.controls.find((item) => item.name === "model").value = "updated-secondary";
  editor.controls.find((item) => item.name === "apiKey").value = "secondary-secret";

  await editor.handlers.submit({ preventDefault() {}, currentTarget: editor });

  const payload = update.mock.calls[0].arguments[0];
  assert.deepEqual(Object.keys(payload), ["llm"]);
  assert.deepEqual(Object.keys(payload.llm), ["models"]);
  assert.equal(payload.llm.models[0].isCurrent, true);
  assert.equal(payload.llm.models[0].apiKey, undefined);
  assert.equal(payload.llm.models[1].model, "updated-secondary");
  assert.equal(payload.llm.models[1].apiKey, "secondary-secret");
});

test("legacy TTS voice normalizes to auto while other unsupported values remain visible", () => {
  const normalized = normalizeModelSettings({ tts: {
    voice: "af_heart",
    models: [{ id: "legacy", voice: "af_heart", isCurrent: true }, { id: "custom", voice: "unsupported-voice", isCurrent: false }],
  } });

  assert.equal(normalized.tts.voice, "auto");
  assert.equal(normalized.tts.models[0].voice, "auto");
  assert.equal(normalized.tts.models[1].voice, "unsupported-voice");
  assert.equal(normalizeModelSettings().tts.voice, "auto");
});

test("Whisper selection reuses a normalized matching ID and sends only sanitized Whisper state", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "tiny", language: "auto", device: "cpu", threads: 4, wordTimestamps: true, runtimeManaged: true, models: [
    { id: "keep-tiny-id", name: "Tiny", provider: "local", model: "local:tiny", isCurrent: true, status: "READY", _runtimeManaged: true },
    { id: "small-id", name: "Small", provider: "local", model: "small", isCurrent: false, status: "READY", hasApiKey: true },
  ] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", label: "Tiny", status: "READY" }, { name: "small", label: "Small", status: "READY" }]);
  const updates = t.mock.method(Api, "updateModelSettings", async (payload) => ({ ...saved, whisper: { ...payload.whisper, runtimeManaged: true } }));
  setupUi();
  await loadSettings();

  await selectCatalogModel("whisper", "small-id");

  const sent = updates.mock.calls[0].arguments[0];
  assert.deepEqual(Object.keys(sent), ["whisper"]);
  assert.equal(sent.whisper.model, "local:small");
  assert.equal(sent.whisper.models.find((item) => item.name === "Tiny").id, "keep-tiny-id");
  assert.equal(sent.whisper.models.filter((item) => item.isCurrent).length, 1);
  assert.equal(sent.whisper.models.find((item) => item.id === "small-id").isCurrent, true);
  assert.equal(sent.whisper.runtimeManaged, undefined);
  assert.equal(sent.whisper.models.some((item) => item.status || item._runtimeManaged || item.hasApiKey), false);
});

test("failed Whisper selection rolls back the selected value when the server does not confirm it", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:tiny", models: [
    { id: "tiny-id", name: "Tiny", provider: "local", model: "tiny", status: "READY", isCurrent: true },
    { id: "small-id", name: "Small", provider: "local", model: "local:small", status: "READY", isCurrent: false },
  ] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", status: "READY" }, { name: "small", status: "READY" }]);
  t.mock.method(Api, "updateModelSettings", async () => ({ ...saved, whisper: structuredClone(saved.whisper) }));
  setupUi({ whisper: true });
  await loadSettings();
  await selectCatalogModel("whisper", "small-id");
  assert.equal(document.nodes.get("#whisperCurrentModel").value, "local:tiny");
});

test("Whisper form saves active card parameters and disabled state using the persisted merge semantics", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:tiny", language: "auto", device: "cpu", threads: 4, wordTimestamps: true, models: [
    { id: "tiny-id", name: "Tiny", provider: "local", model: "local:tiny", enabled: true, language: "auto", device: "cpu", threads: 4, wordTimestamps: true, isCurrent: true },
  ] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", status: "READY" }]);
  let persisted;
  t.mock.method(Api, "updateModelSettings", async (payload) => {
    const active = payload.whisper.models.find((item) => item.isCurrent);
    persisted = { ...payload.whisper, ...active };
    return { ...saved, whisper: persisted };
  });
  const { form } = setupUi({ whisper: true });
  await loadSettings();
  form.handlers.submit = form.handlers.submit || (() => {});
  const whisperForm = document.lists.get("[data-model-form]").find((item) => item.dataset.modelForm === "whisper");
  whisperForm.controls.find((item) => item.name === "enabled").checked = false;
  whisperForm.controls.find((item) => item.name === "threads").value = "8";
  whisperForm.controls.find((item) => item.name === "device").value = "mps";
  await whisperForm.handlers.submit({ preventDefault() {} });
  assert.equal(persisted.enabled, false);
  assert.equal(persisted.threads, 8);
  assert.equal(persisted.device, "mps");
  assert.equal(persisted.models[0].threads, 8);
  assert.equal(persisted.models[0].enabled, false);
});

test("inventory polling updates readiness without overwriting unsaved Whisper parameter edits", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:tiny", threads: 4, language: "auto", device: "cpu", wordTimestamps: true, models: [] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", status: "READY" }]);
  setupUi({ whisper: true });
  const whisperStatus = new NodeMock();
  document.nodes.set("#modelStatusWhisper", whisperStatus);
  await loadSettings();
  const form = document.lists.get("[data-model-form]").find((item) => item.dataset.modelForm === "whisper");
  const threads = form.controls.find((item) => item.name === "threads");
  threads.value = "19";
  form.handlers.input();
  document.dispatchEvent(new CustomEvent("whisper-models-change", { detail: {
    items: [{ name: "tiny", status: "ERROR" }],
    whisper: { ...saved.whisper, status: "ERROR" },
  } }));
  assert.equal(threads.value, "19");
  assert.equal(whisperStatus.textContent, "检查失败");
});

test("Whisper readiness text distinguishes error, downloading, missing, and not installed", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:tiny", threads: 4, models: [] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", status: "READY" }]);
  setupUi({ whisper: true });
  const status = new NodeMock();
  document.nodes.set("#modelStatusWhisper", status);
  await loadSettings();
  for (const [inventoryStatus, expected] of [["ERROR", "检查失败"], ["DOWNLOADING", "下载中"], ["MISSING", "当前模型缺失"], ["NOT_INSTALLED", "未安装"]]) {
    document.dispatchEvent(new CustomEvent("whisper-models-change", { detail: {
      items: [{ name: "tiny", status: inventoryStatus }],
      whisper: { ...saved.whisper, status: inventoryStatus },
    } }));
    assert.equal(status.textContent, expected);
  }
});

test("a saved Whisper model stays visibly selected and disabled when inventory reports it unavailable", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:whisper-tiny", threads: 4, models: [] };
  t.mock.method(Api, "getModelSettings", async () => structuredClone(saved));
  t.mock.method(Api, "listLocalModels", async () => [
    { name: "tiny.en", label: "Whisper Tiny English", status: "READY" },
    { name: "whisper-tiny", label: "Whisper Tiny", status: "ERROR" },
  ]);
  setupUi({ whisper: true });
  await loadSettings();

  const select = document.nodes.get("#whisperCurrentModel");
  const selectedOption = select.children.find((option) => option.value === "local:whisper-tiny");
  assert.equal(select.value, "local:whisper-tiny");
  assert.equal(selectedOption.disabled, true);
  assert.match(selectedOption.textContent, /检查失败/);
  assert.equal(select.children.find((option) => option.value === "local:tiny.en").disabled, false);

  // When the failed asset is removed from inventory, its persisted selection remains explicit as MISSING.
  document.dispatchEvent(new CustomEvent("whisper-models-change", { detail: {
    items: [{ name: "tiny.en", label: "Whisper Tiny English", status: "READY" }],
    whisper: { ...saved.whisper, status: "MISSING" },
  } }));
  const missingOption = select.children.find((option) => option.value === "local:whisper-tiny");
  assert.equal(select.value, "local:whisper-tiny");
  assert.equal(missingOption.disabled, true);
  assert.match(missingOption.textContent, /缺失/);
});

test("a failed settings refresh clears stale READY choices but preserves the saved model as disabled", async (t) => {
  const saved = baseSettings();
  saved.whisper = { enabled: true, provider: "local", model: "local:tiny", threads: 4, models: [] };
  let getCount = 0;
  t.mock.method(Api, "getModelSettings", async () => {
    getCount += 1;
    if (getCount > 1) throw new Error("offline");
    return structuredClone(saved);
  });
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", label: "Tiny", status: "READY" }]);
  setupUi({ whisper: true });
  await loadSettings();
  const select = document.nodes.get("#whisperCurrentModel");
  assert.equal(select.children.find((option) => option.value === "local:tiny").disabled, false);

  document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: "model-settings" } }));
  await new Promise((resolve) => setImmediate(resolve));

  const current = select.children.find((option) => option.value === "local:tiny");
  assert.equal(select.value, "local:tiny");
  assert.equal(current.disabled, true);
  assert.match(current.textContent, /缺失/);
});

test("model panels follow store modelKind changes from browser history without pushing another kind", () => {
  setupUi();
  const llm = new NodeMock();
  llm.dataset.modelPanel = "llm";
  const whisper = new NodeMock();
  whisper.dataset.modelPanel = "whisper";
  document.lists.set("[data-model-panel]", [llm, whisper]);
  // Store changes are the router's popstate signal; UI sync must not call the setter again.
  setModelKind("whisper", { history: false });
  assert.equal(whisper.hidden, false);
  assert.equal(llm.hidden, true);
  setModelKind("llm", { history: false });
  assert.equal(llm.hidden, false);
  assert.equal(whisper.hidden, true);
});
