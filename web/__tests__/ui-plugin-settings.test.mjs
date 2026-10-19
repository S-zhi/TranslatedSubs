import { test } from "node:test";
import assert from "node:assert/strict";
import { createPluginSettingsController, pluginConfigFields, pluginInstanceCard } from "../js/ui-plugin-settings.js";
import { pluginBindingOptions, parsePluginConfig, refsForBinding, pluginErrorMessage } from "../js/plugin-settings.js";
import { createMockPluginApi } from "../js/mock-plugins.js";

class NodeMock {
  constructor() { this.dataset = {}; this.handlers = {}; this.value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.textContent = ""; this.innerHTML = ""; this.classList = { toggle() {} }; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
  focus() { this.focused = true; }
  querySelectorAll() { return []; }
  insertAdjacentHTML(_position, value) { this.innerHTML += value; }
}
const plugin = { plugin_id: "builtin.translation_engine", implementation_version: "1", stage: "translate", name: "翻译引擎", config_schema_version: 1, config_schema: { batch_size: { type: "integer", label: "翻译批量", minimum: 1, maximum: 200, default: 20 } }, bindings: ["engine", "llm"], operations: ["translate"], capabilities: [] };
const instance = { id: "default_translate", name: "默认翻译", stage: "translate", plugin_id: plugin.plugin_id, implementation_version: "1", config_schema_version: 1, enabled: true, config: { batch_size: 5 }, model_ref: null, engine_ref: null };

function fixture() {
  const nodes = new Map();
  for (const selector of ["#pluginStageList", "#pluginSettingsFeedback", "#pluginSettingsRevision", "#pluginInstanceEditor", "#pluginInstanceForm", "#pluginEditorFeedback", "#pluginEditorTitle", "#pluginConfigFields", "#pluginBinding", "#pluginBindingHint", "#pluginSave", "#pluginDeleteConfirm", "#pluginAdd", "#pluginRefresh", "#pluginEditorRefresh", "#pluginEditorCancel", "#pluginDeleteCancel", "#pluginDeleteAccept", "#pluginDeleteMessage", "#pluginVersionHint"]) nodes.set(selector, new NodeMock());
  const form = nodes.get("#pluginInstanceForm");
  form.elements = Object.fromEntries(["id", "name", "stage", "plugin", "enabled"].map((key) => [key, new NodeMock()]));
  const pluginNode = form.elements.plugin;
  Object.defineProperty(pluginNode, "innerHTML", { set(value) { this.html = value; this.value = JSON.stringify([plugin.plugin_id, "1"]); }, get() { return this.html || ""; } });
  const config = new NodeMock();
  config.dataset.pluginConfig = "batch_size";
  config.value = "17";
  nodes.get("#pluginConfigFields").querySelectorAll = (selector) => selector === "[data-plugin-config]" ? [config] : [];
  const root = new NodeMock();
  root.querySelector = (selector) => nodes.get(selector);
  let current = { revision: 7, instances: [structuredClone(instance)], defaults: { translate: instance.id } };
  const writes = [];
  const api = {
    getPluginCatalog: async () => ({ plugins: [plugin] }),
    listPluginInstances: async () => structuredClone(current),
    getModelSettings: async () => ({ llm: { models: [{ id: "stable-card", name: "翻译模型", enabled: true, apiKey: "never-copy-this" }] } }),
    listTranslationEngines: async () => [{ id: "engine-main", name: "Main", enabled: true }],
    getPluginInstance: async () => ({ revision: current.revision, instance: structuredClone(instance) }),
    updatePluginInstance: async (id, record, revision) => { writes.push({ id, record, revision }); current = { ...current, revision: revision + 1, instances: [record] }; return { revision: current.revision, instance: record }; },
    createPluginInstance: async (record, revision) => { writes.push({ record, revision }); return { revision: revision + 1, instance: record }; },
  };
  const controller = createPluginSettingsController({ root, api });
  return { nodes, form, api, writes, controller, setCurrent: (value) => { current = value; } };
}

test("scalar schema fields preserve false/zero, reject ranges, omit credentials and bind stable IDs", () => {
  const schema = { threads: { type: "integer", label: "线程", minimum: 1, maximum: 8 }, speed: { type: "number", label: "速度", minimum: 0 }, enabled: { type: "boolean", label: "开关" }, backend: { type: "string", enum: ["local", "http"] }, api_key: { type: "string" } };
  const html = pluginConfigFields(schema, { enabled: false, speed: 0 });
  assert.match(html, /value="false" selected/);
  assert.match(html, /value="0"/);
  assert.doesNotMatch(html, /api_key/);
  assert.deepEqual(parsePluginConfig(schema, { threads: "4", speed: "0", enabled: "false", backend: '"http"', api_key: "secret" }).config, { threads: 4, speed: 0, enabled: false, backend: "http" });
  assert.match(parsePluginConfig(schema, { threads: "9" }).errors.threads, /1 至 8/);
  assert.match(parsePluginConfig(schema, { threads: "1.5" }).errors.threads, /整数/);
  const candidates = pluginBindingOptions(plugin, { llm: { models: [{ name: "no ID" }, { id: "card:stable", name: "model" }] } }, [{ id: "engine-1", name: "engine" }]);
  assert.equal(candidates.length, 2);
  assert.deepEqual(refsForBinding("llm:card:stable"), { model_ref: { kind: "llm", card_id: "card:stable" }, engine_ref: null });
  assert.deepEqual(refsForBinding(""), { model_ref: null, engine_ref: null });
});

test("cobalt metadata renders and parses all scalar fields while keeping secret filters", async () => {
  const { plugins } = await createMockPluginApi({ storage: () => null }).getPluginCatalog();
  const { config_schema: schema } = plugins.find((item) => item.plugin_id === "builtin.cobalt");
  assert.deepEqual(Object.keys(schema), ["endpoint", "socket_timeout", "access_mode", "fallback_policy", "filename_style", "audio_format"]);
  const config = { endpoint: "http://127.0.0.1:9000/", socket_timeout: 45, access_mode: "api_key", fallback_policy: "ytdlp", filename_style: "pretty", audio_format: "opus" };
  const fields = { ...schema, api_key: { type: "string" }, use_api_key: { type: "boolean" }, credential_ref: { type: "string" } };
  const html = pluginConfigFields(fields, { ...config, api_key: "never-copy-this", credential_ref: "settings:cobalt_api_key" });
  for (const key of Object.keys(schema)) assert.match(html, new RegExp(`data-plugin-config="${key}"`));
  assert.match(html, /data-plugin-config="endpoint"[^>]*type="text"[^>]*value="http:\/\/127\.0\.0\.1:9000\/"/);
  assert.match(html, /data-plugin-config="socket_timeout"[^>]*type="number"[^>]*min="1"[^>]*max="300"/);
  assert.match(html, /value="&quot;api_key&quot;" selected/);
  assert.match(html, /value="&quot;ytdlp&quot;" selected/);
  assert.doesNotMatch(html, /data-plugin-config="(?:api_key|use_api_key|credential_ref)"|never-copy-this|settings:cobalt_api_key/);
  const values = { ...config, endpoint: `  ${config.endpoint}  `, socket_timeout: "45", access_mode: '"api_key"', fallback_policy: '"ytdlp"', filename_style: '"pretty"', audio_format: '"opus"', api_key: "never-copy-this" };
  assert.deepEqual(parsePluginConfig(fields, values), { config, errors: {} });
  assert.match(parsePluginConfig(schema, {}).errors.endpoint, /不能为空/);
  assert.match(parsePluginConfig(schema, { endpoint: config.endpoint, socket_timeout: "0" }).errors.socket_timeout, /1 至 300/);
  assert.match(parsePluginConfig(schema, { endpoint: config.endpoint, access_mode: '"bearer"' }).errors.access_mode, /有效选项/);
});

test("cobalt selection uses the existing instance editor and saves auth mode through CAS", async () => {
  const { controller, api, nodes, form, writes } = fixture();
  const catalog = await createMockPluginApi({ storage: () => null }).getPluginCatalog();
  const cobalt = catalog.plugins.find((item) => item.plugin_id === "builtin.cobalt");
  api.getPluginCatalog = async () => catalog;
  await controller.refresh();
  controller.openDraft(null, "download");
  assert.match(form.elements.plugin.innerHTML, /Cobalt 下载/);
  form.elements.plugin.value = JSON.stringify([cobalt.plugin_id, "1"]);
  form.elements.plugin.handlers.change();
  assert.equal(nodes.get("#pluginBinding").disabled, true);
  assert.match(nodes.get("#pluginConfigFields").innerHTML, /data-plugin-config="access_mode"/);
  const values = { endpoint: "http://127.0.0.1:9000/", socket_timeout: "45", access_mode: '"api_key"', fallback_policy: '"none"', filename_style: '"basic"', audio_format: '"best"' };
  const controls = Object.entries(values).map(([key, value]) => { const node = new NodeMock(); node.dataset.pluginConfig = key; node.value = value; return node; });
  nodes.get("#pluginConfigFields").querySelectorAll = (selector) => selector === "[data-plugin-config]" ? controls : [];
  form.elements.id.value = "cobalt-local";
  form.elements.name.value = "本机 cobalt";
  assert.equal(await controller.save(), true);
  assert.equal(writes[0].revision, 7);
  assert.equal(writes[0].record.plugin_id, "builtin.cobalt");
  assert.deepEqual(writes[0].record.config, { endpoint: values.endpoint, socket_timeout: 45, access_mode: "api_key", fallback_policy: "none", filename_style: "basic", audio_format: "best" });
  assert.equal(writes[0].record.model_ref, null);
  assert.equal(writes[0].record.engine_ref, null);
});

test("catalog renders all seven stages and protected defaults explain their disabled actions", async () => {
  const { nodes, controller } = fixture();
  await controller.refresh();
  const html = nodes.get("#pluginStageList").innerHTML;
  assert.equal((html.match(/data-plugin-stage=/g) || []).length, 7);
  assert.match(html, /翻译引擎/);
  assert.match(html, /请先切换阶段默认实例/);
  const card = pluginInstanceCard({ ...instance, name: '<img onerror="bad">' }, plugin, false, []);
  assert.doesNotMatch(card, /<img/);
  assert.match(card, /沿用任务 \/ 当前模型/);
});

test("editor saves full instance with stable binding reference and no copied model credentials", async () => {
  const { controller, nodes, form, writes } = fixture();
  await controller.refresh();
  controller.openDraft(instance);
  form.elements.name.value = "工作翻译";
  nodes.get("#pluginBinding").value = "llm:stable-card";
  assert.equal(await controller.save(), true);
  assert.equal(writes[0].revision, 7);
  assert.equal(writes[0].record.name, "工作翻译");
  assert.deepEqual(writes[0].record.config, { batch_size: 17 });
  assert.deepEqual(writes[0].record.model_ref, { kind: "llm", card_id: "stable-card" });
  assert.equal(writes[0].record.engine_ref, null);
  assert.doesNotMatch(JSON.stringify(writes[0].record), /apiKey|never-copy/);
});

test("409 keeps draft inputs, blocks resave until refresh, then uses refreshed revision", async () => {
  const { controller, api, nodes, form, writes, setCurrent } = fixture();
  await controller.refresh();
  controller.openDraft(instance);
  form.elements.name.value = "尚未保存的名字";
  const originalUpdate = api.updatePluginInstance;
  api.updatePluginInstance = async () => { const error = new Error("新版本"); error.status = 409; throw error; };
  assert.equal(await controller.save(), false);
  assert.equal(form.elements.name.value, "尚未保存的名字");
  assert.match(nodes.get("#pluginEditorFeedback").textContent, /输入已保留/);
  assert.equal(nodes.get("#pluginSave").disabled, true);
  setCurrent({ revision: 12, instances: [instance], defaults: { translate: instance.id } });
  assert.equal(await controller.refresh(), true);
  assert.equal(form.elements.name.value, "尚未保存的名字");
  api.updatePluginInstance = originalUpdate;
  assert.equal(await controller.save(), true);
  assert.equal(writes[0].revision, 12);
});

test("422 validation and 503 remain retryable without resetting the editor", async () => {
  const { controller, api, nodes, form } = fixture();
  await controller.refresh();
  controller.openDraft(instance);
  form.elements.name.value = "Retry this";
  for (const status of [422, 503]) {
    api.updatePluginInstance = async () => { const error = new Error("字段或服务错误"); error.status = status; throw error; };
    assert.equal(await controller.save(), false);
    assert.equal(form.elements.name.value, "Retry this");
    assert.equal(nodes.get("#pluginSave").disabled, false);
    assert.match(nodes.get("#pluginEditorFeedback").textContent, status === 422 ? /校验失败/ : /可重试/);
  }
  assert.match(pluginErrorMessage({ status: 503 }), /可重试/);
});

test("Whisper bindings clear backend/address overrides and preserve card-owned runtime source", async () => {
  const { controller, api, nodes, form, writes } = fixture();
  const whisperPlugin = { ...plugin, plugin_id: "builtin.whisper", stage: "transcribe", bindings: ["whisper"], config_schema: { backend: { type: "string", label: "后端" }, service_url: { type: "string", label: "服务地址" }, beam_size: { type: "integer", minimum: 1, maximum: 20 } } };
  api.getPluginCatalog = async () => ({ plugins: [whisperPlugin] });
  api.getModelSettings = async () => ({ whisper: { models: [{ id: "whisper-fixed", name: "Fixed", enabled: true, status: "READY" }] } });
  await controller.refresh();
  controller.openDraft(null, "transcribe");
  form.elements.plugin.value = JSON.stringify([whisperPlugin.plugin_id, "1"]);
  form.elements.plugin.handlers.change();
  const controls = ["backend", "service_url", "beam_size"].map((key, index) => { const node = new NodeMock(); node.dataset.pluginConfig = key; node.value = ["http", "https://old.example", "5"][index]; return node; });
  nodes.get("#pluginConfigFields").querySelectorAll = (selector) => selector === "[data-plugin-config]" ? controls : [];
  nodes.get("#pluginBinding").value = "whisper:whisper-fixed";
  nodes.get("#pluginBinding").handlers.change();
  assert.equal(controls[0].disabled, true);
  assert.equal(controls[0].value, "");
  assert.equal(controls[1].disabled, true);
  assert.equal(controls[1].value, "");
  assert.match(nodes.get("#pluginBindingHint").textContent, /由绑定的 Whisper 卡提供/);
  form.elements.id.value = "whisper-instance";
  form.elements.name.value = "Whisper";
  assert.equal(await controller.save(), true);
  assert.deepEqual(writes[0].record.config, { beam_size: 5 });
  assert.deepEqual(writes[0].record.model_ref, { kind: "whisper", card_id: "whisper-fixed" });
});

test("a successful create followed by a failed read does not leave a duplicate POST draft", async () => {
  const { controller, api, nodes, form, writes } = fixture();
  await controller.refresh();
  controller.openDraft(null, "translate");
  form.elements.id.value = "new-instance";
  form.elements.name.value = "New";
  api.listPluginInstances = async () => { throw new Error("read unavailable"); };
  assert.equal(await controller.save(), true);
  assert.equal(nodes.get("#pluginInstanceEditor").hidden, true);
  assert.match(nodes.get("#pluginSettingsFeedback").textContent, /列表刷新失败/);
  assert.equal(await controller.save(), false);
  assert.equal(writes.length, 1);
});
