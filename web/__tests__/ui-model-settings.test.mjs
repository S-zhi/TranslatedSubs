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
  addEventListener(name, handler) { this.handlers[name] = handler; }
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
  constructor() { this.nodes = new Map(); this.lists = new Map(); this.handlers = {}; }
  querySelector(selector) { return this.nodes.get(selector) || null; }
  querySelectorAll(selector) { return this.lists.get(selector) || []; }
  createElement(tag) { const node = new NodeMock(); node.tagName = tag.toUpperCase(); return node; }
  createTextNode(text) { return { textContent: text }; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
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
  localStorage: { getItem() { return null; }, setItem() {} },
};
globalThis.document = new DocumentMock();
globalThis.FormData = FormDataMock;
globalThis.requestAnimationFrame = (callback) => callback();

const { Api } = await import("../js/api.js");
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

function setupUi({ create = false, cards = false, modelId = "" } = {}) {
  globalThis.document = new DocumentMock();
  const root = new NodeMock();
  root.querySelectorAll = (selector) => document.lists.get(selector) || [];
  document.nodes.set("#modelSettings", root);
  const form = configureForm("llm", [
    control("enabled", true, "checkbox"), control("provider", "deepseek", "select"), control("model", "old-model"),
  ]);
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
