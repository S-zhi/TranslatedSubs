import { test } from "node:test";
import assert from "node:assert/strict";
import { PLUGIN_STAGES, resolveTaskPluginBindings, selectedPluginInstances, taskPluginStages } from "../js/plugin-settings.js";
import { collectTaskPluginInstances, createTaskPluginController } from "../js/task-plugins.js";

class NodeMock {
  constructor(value = "") { this.value = value; this.dataset = {}; this.disabled = false; this.handlers = {}; this.classList = { toggle() {} }; this.options = []; this.html = ""; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
  set innerHTML(value) { this.html = value; }
  get innerHTML() { return this.html; }
  append(option) { this.options.push(option); option.remove = () => { this.options = this.options.filter((item) => item !== option); }; }
}

function snapshot() {
  const instances = PLUGIN_STAGES.map(({ id }) => ({ id: `default_${id}`, name: id, stage: id, enabled: true, config: {}, model_ref: null, engine_ref: null }));
  return { revision: 4, instances, defaults: Object.fromEntries(instances.map((instance) => [instance.stage, instance.id])) };
}

function fixture() {
  const nodes = new Map(["#taskPluginStages", "#taskPluginFeedback", "#taskPluginRefresh", "#model", "#engine", "#whisperModelHint", "#taskEngineBindingHint", "#taskTtsBindingHint", "#url"].map((key) => [key, new NodeMock()]));
  nodes.get("#url").dataset.sourceType = "url";
  nodes.get("#model").append(new NodeMock("local:tiny"));
  nodes.get("#model").value = "local:tiny";
  nodes.get("#engine").append(new NodeMock("local-opus-en-zh"));
  nodes.get("#engine").value = "local-opus-en-zh";
  const voice = new NodeMock("auto");
  voice.append(new NodeMock("auto"));
  voice.append(new NodeMock("af_maple"));
  const form = new NodeMock();
  form.elements = { needSubtitle: { value: "on" }, ttsEnabled: { checked: true }, ttsVoice: voice, videoFile: { files: [] } };
  form.querySelector = (selector) => nodes.get(selector) || null;
  form.querySelectorAll = () => [];
  const root = new NodeMock();
  root.querySelector = form.querySelector;
  const state = snapshot();
  const models = { whisper: { models: [{ id: "whisper-card", model: "local:small", name: "Small fixed", enabled: true, status: "READY" }] }, llm: { models: [{ id: "llm-card", model: "deepseek-chat", name: "LLM fixed", enabled: true }] }, tts: { models: [{ id: "tts-card", name: "TTS fixed", enabled: true, voice: "af_maple" }] } };
  const engines = [{ id: "local-opus-en-zh", name: "Local", enabled: true, modelStatus: "NOT_INSTALLED" }, { id: "cloud", name: "Cloud", enabled: true }];
  const api = { listPluginInstances: async () => structuredClone(state), getModelSettings: async () => structuredClone(models), listTranslationEngines: async () => structuredClone(engines) };
  const controller = createTaskPluginController({ root, form, api, getWhisperState: () => ({ ok: false, locked: false, message: "no current ready" }) });
  return { state, models, engines, api, nodes, voice, form, controller };
}

globalThis.document = { addEventListener() {}, createElement: () => new NodeMock() };

test("task stages hide download on upload, subtitle stages for download-only and TTS until requested", () => {
  assert.deepEqual(taskPluginStages({ needSubtitle: false }), ["download"]);
  assert.deepEqual(taskPluginStages({ sourceType: "upload", needSubtitle: false }), []);
  assert.equal(taskPluginStages({ needSubtitle: true, ttsEnabled: false }).includes("tts"), false);
  assert.equal(taskPluginStages({ needSubtitle: true, ttsEnabled: true }).includes("tts"), true);
  assert.deepEqual(selectedPluginInstances({ download: "download-1", tts: "tts-1", transcribe: "whisper-1", invalid: "x" }, { sourceType: "upload", needSubtitle: true, ttsEnabled: false }), { transcribe: "whisper-1" });
});

test("invalid explicit selections and deleted bindings never fall back to current cards", () => {
  const state = snapshot();
  state.instances.find((item) => item.stage === "translate").model_ref = { kind: "llm", card_id: "deleted" };
  const result = resolveTaskPluginBindings({ snapshot: state, selections: { transcribe: "missing" }, settings: { llm: { models: [{ id: "current", isCurrent: true }] } } });
  assert.equal(result.resolved.transcribe, undefined);
  assert.equal(result.resolved.translate.model, null);
  assert.match(result.errors.join(" "), /missing/);
  assert.match(result.errors.join(" "), /模型已不存在/);
});

test("default fixed bindings lock controls, identify provenance and replace contradictory legacy payload values", async () => {
  const { state, nodes, voice, controller } = fixture();
  state.instances.find((item) => item.stage === "transcribe").model_ref = { kind: "whisper", card_id: "whisper-card" };
  state.instances.find((item) => item.stage === "translate").model_ref = { kind: "llm", card_id: "llm-card" };
  state.instances.find((item) => item.stage === "tts").model_ref = { kind: "tts", card_id: "tts-card" };
  await controller.refresh();
  assert.equal(controller.isReady(), true);
  assert.equal(controller.whisperReady(), true);
  assert.equal(controller.llmBound(), true);
  assert.equal(nodes.get("#model").value, "local:small");
  assert.equal(nodes.get("#model").disabled, true);
  assert.equal(nodes.get("#engine").value, "plugin-bound-llm");
  assert.equal(voice.value, "af_maple");
  assert.equal(voice.disabled, true);
  assert.match(nodes.get("#whisperModelHint").textContent, /阶段默认插件.*Small fixed/);
  const payload = controller.applyPayload({ model: "local:tiny", engine: "local-opus-en-zh", ttsVoice: "auto" });
  assert.deepEqual(payload, { model: "local:small", engine: "deepseek", ttsVoice: "af_maple", pluginInstances: {} });
  assert.match(nodes.get("#taskPluginStages").innerHTML, /跟随默认/);
});

test("download fingerprint changes for selected instance edits and default changes", async () => {
  const { state, nodes, controller } = fixture();
  await controller.refresh();
  const initial = controller.downloadFingerprint();
  state.instances.push({ ...state.instances.find((item) => item.stage === "download"), id: "proxy-download", config: { proxy: "http://proxy.example:8080" } });
  await controller.refresh();
  assert.equal(controller.downloadFingerprint(), initial, "unrelated instance insertion preserves probe");
  nodes.get("#taskPluginStages").handlers.change({ target: { dataset: { taskPluginStage: "download" }, value: "proxy-download" } });
  const selected = controller.downloadFingerprint();
  assert.notEqual(selected, initial);
  state.instances.find((item) => item.id === "proxy-download").config.socket_timeout = 7;
  await controller.refresh();
  assert.notEqual(controller.downloadFingerprint(), selected, "editing same instance invalidates probe");
  nodes.get("#taskPluginStages").handlers.change({ target: { dataset: { taskPluginStage: "download" }, value: "" } });
  assert.equal(controller.downloadFingerprint(), initial);
  state.defaults.download = "proxy-download";
  await controller.refresh();
  assert.notEqual(controller.downloadFingerprint(), initial, "following a new default invalidates probe");
});

test("removing bindings restores previous task values and model readiness locks", async () => {
  const { state, nodes, voice, controller } = fixture();
  const translation = state.instances.find((item) => item.stage === "translate");
  const transcribe = state.instances.find((item) => item.stage === "transcribe");
  const tts = state.instances.find((item) => item.stage === "tts");
  translation.engine_ref = { id: "cloud" };
  transcribe.model_ref = { kind: "whisper", card_id: "whisper-card" };
  tts.model_ref = { kind: "tts", card_id: "tts-card" };
  await controller.refresh();
  assert.equal(nodes.get("#engine").value, "cloud");
  translation.engine_ref = null;
  transcribe.model_ref = null;
  tts.model_ref = null;
  await controller.refresh();
  assert.equal(nodes.get("#engine").value, "local-opus-en-zh");
  assert.equal(nodes.get("#engine").disabled, false);
  assert.equal(nodes.get("#model").value, "local:tiny");
  assert.equal(nodes.get("#model").disabled, true);
  assert.equal(voice.value, "auto");
  assert.equal(voice.disabled, false);
});

test("explicit instance changes are submitted while unselected stages keep following default", async () => {
  const { state, nodes, controller } = fixture();
  state.instances.push({ ...state.instances.find((item) => item.stage === "translate"), id: "cloud-instance", name: "Cloud instance", engine_ref: { id: "cloud" } });
  await controller.refresh();
  nodes.get("#taskPluginStages").handlers.change({ target: { dataset: { taskPluginStage: "translate" }, value: "cloud-instance" } });
  assert.equal(nodes.get("#engine").value, "cloud");
  assert.match(nodes.get("#taskEngineBindingHint").textContent, /任务指定插件/);
  assert.deepEqual(controller.applyPayload({}).pluginInstances, { translate: "cloud-instance" });
  state.instances = state.instances.filter((item) => item.id !== "cloud-instance");
  await controller.refresh();
  assert.equal(controller.isReady(), false);
  assert.match(controller.error(), /cloud-instance/);
});

test("HTTP/custom transcription delegates readiness to plugin preflight without local inventory", async () => {
  const { state, controller } = fixture();
  const transcribe = state.instances.find((item) => item.stage === "transcribe");
  transcribe.plugin_id = "builtin.whisper";
  for (const backend of ["http", "custom", "custom_http"]) {
    transcribe.config = { backend, service_url: "https://asr.example/transcribe" };
    await controller.refresh();
    assert.equal(controller.whisperReady(), true, backend);
    assert.equal(controller.requiresWhisperInventory(), false, backend);
  }
  for (const config of [{}, { backend: "local_whisper" }]) {
    transcribe.config = config;
    await controller.refresh();
    assert.equal(controller.whisperReady(), false);
    assert.equal(controller.requiresWhisperInventory(), true);
  }
  transcribe.plugin_id = "custom.remote_asr";
  transcribe.config = {};
  await controller.refresh();
  assert.equal(controller.whisperReady(), true);
  assert.equal(controller.requiresWhisperInventory(), false);
});

test("bound local Whisper retains card readiness even with stale remote configuration", async () => {
  const { state, models, controller } = fixture();
  const transcribe = state.instances.find((item) => item.stage === "transcribe");
  transcribe.plugin_id = "builtin.whisper";
  transcribe.config = { backend: "http" };
  transcribe.model_ref = { kind: "whisper", card_id: "whisper-card" };
  models.whisper.models[0].status = "NOT_INSTALLED";
  await controller.refresh();
  assert.equal(controller.whisperReady(), false);
  assert.equal(controller.requiresWhisperInventory(), false);
  models.whisper.models[0].status = "READY";
  await controller.refresh();
  assert.equal(controller.whisperReady(), true);
});

test("collectTaskPluginInstances sends only visible explicit values and retry errors block stale submission", async () => {
  const { api, form, controller } = fixture();
  form.querySelectorAll = () => [{ dataset: { taskPluginStage: "download" }, value: "download-x" }, { dataset: { taskPluginStage: "tts" }, value: "tts-x" }, { dataset: { taskPluginStage: "translate" }, value: "" }];
  form.elements.ttsEnabled.checked = false;
  assert.deepEqual(collectTaskPluginInstances(form), { download: "download-x" });
  await controller.refresh();
  api.listPluginInstances = async () => { throw new Error("service unavailable"); };
  assert.equal(await controller.refresh(), false);
  assert.equal(controller.isReady(), false);
  assert.match(controller.error(), /刷新后重试/);
});
