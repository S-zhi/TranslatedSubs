import { test } from "node:test";
import assert from "node:assert/strict";

const values = new Map();
globalThis.localStorage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
globalThis.window = { localStorage, APP_CONFIG: { USE_MOCK: true } };
const { Api } = await import("../js/api.js");
const { createMockPluginApi } = await import("../js/mock-plugins.js");

test("MockAPI exposes eight implementations with seven defaults, CAS CRUD and atomic defaults patch without credentials", async () => {
  values.clear();
  const { plugins } = await Api.getPluginCatalog();
  assert.equal(plugins.length, 8);
  assert.deepEqual([...new Set(plugins.map((plugin) => plugin.stage))], ["download", "audio_extract", "vocal_separate", "transcribe", "translate", "tts", "render"]);
  assert.deepEqual(plugins.filter((plugin) => plugin.stage === "download").map((plugin) => plugin.plugin_id), ["builtin.ytdlp", "builtin.cobalt"]);
  const original = await Api.listPluginInstances();
  assert.equal(original.instances.length, 7);
  assert.equal(new Set(original.instances.map((instance) => instance.id)).size, 7);
  assert.equal(Object.keys(original.defaults).length, 7);
  assert.equal(original.instances.find((instance) => instance.id === original.defaults.download).plugin_id, "builtin.ytdlp");
  const base = original.instances.find((instance) => instance.stage === "translate");
  const created = await Api.createPluginInstance({ ...base, id: "translator-secondary", name: "副翻译", config: { batch_size: 4 } }, original.revision);
  assert.equal(created.revision, original.revision + 1);
  await assert.rejects(Api.updatePluginInstance(created.instance.id, { name: "旧版本" }, original.revision), (error) => error.status === 409);
  const otherClient = createMockPluginApi();
  assert.equal((await otherClient.getPluginInstance(created.instance.id)).instance.name, "副翻译");
  const changed = await Api.updatePluginDefaults({ translate: created.instance.id }, created.revision);
  assert.equal(changed.defaults.download, original.defaults.download);
  await assert.rejects(Api.deletePluginInstance(created.instance.id, changed.revision), (error) => error.status === 409);
  await assert.rejects(Api.updatePluginInstance(created.instance.id, { enabled: false }, changed.revision), (error) => error.status === 409);
  const restored = await Api.updatePluginDefaults({ translate: base.id }, changed.revision);
  const disabled = await Api.updatePluginInstance(created.instance.id, { enabled: false }, restored.revision);
  assert.equal(disabled.instance.enabled, false);
  const deleted = await Api.deletePluginInstance(created.instance.id, disabled.revision);
  assert.equal(deleted.ok, true);
  await assert.rejects(Api.getPluginInstance(created.instance.id), (error) => error.status === 404);
});

test("Mock validates schema values, exclusive stable bindings and rejects inline credentials", async () => {
  values.clear();
  const original = await Api.listPluginInstances();
  const translate = original.instances.find((instance) => instance.stage === "translate");
  await assert.rejects(Api.createPluginInstance({ ...translate, id: "bad", name: "bad", config: { batch_size: 0 } }, original.revision), (error) => error.status === 422);
  await assert.rejects(Api.createPluginInstance({ ...translate, id: "bad", name: "bad", config: { apiKey: "secret" } }, original.revision), (error) => error.status === 422);
  await assert.rejects(Api.createPluginInstance({ ...translate, id: "bad", name: "bad", model_ref: { kind: "llm", card_id: "missing" } }, original.revision), (error) => error.status === 422);
  const model = await Api.createModelSettings("llm", { name: "Bound model", provider: "deepseek", model: "deepseek-chat", enabled: true, apiKey: "secret" });
  const bound = await Api.createPluginInstance({ ...translate, id: "bound", name: "Bound", model_ref: { kind: "llm", card_id: model.model.id } }, original.revision);
  assert.deepEqual(bound.instance.model_ref, { kind: "llm", card_id: model.model.id });
  assert.doesNotMatch(JSON.stringify(bound.instance), /secret|apiKey/);
  await assert.rejects(Api.updatePluginInstance("bound", { engine_ref: { id: "local-opus-en-zh" } }, bound.revision), (error) => error.status === 422);
});

test("Mock cobalt instances accept scalar options without changing defaults or admitting inline credentials", async () => {
  const api = createMockPluginApi({ storage: () => null });
  const initial = await api.listPluginInstances();
  const { plugins } = await api.getPluginCatalog();
  const cobalt = plugins.find((plugin) => plugin.plugin_id === "builtin.cobalt");
  assert.deepEqual(cobalt.operations, ["download"]);
  assert.deepEqual(cobalt.capabilities, ["probe"]);
  assert.deepEqual(cobalt.bindings, []);
  assert.equal(cobalt.config_schema.endpoint.required, true);
  assert.equal(cobalt.config_schema.access_mode.default, "anonymous");
  assert.equal(cobalt.config_schema.fallback_policy.default, "none");
  const record = {
    id: "cobalt-local", name: "本机 cobalt", stage: "download", plugin_id: cobalt.plugin_id,
    implementation_version: "1", config_schema_version: 1, enabled: true,
    config: { endpoint: "http://127.0.0.1:9000/", socket_timeout: 45, access_mode: "api_key", fallback_policy: "none", filename_style: "pretty", audio_format: "opus" },
  };
  const created = await api.createPluginInstance(record, initial.revision);
  assert.deepEqual(created.instance.config, record.config);
  assert.equal(created.revision, initial.revision + 1);
  const snapshot = await api.listPluginInstances();
  assert.equal(snapshot.instances.length, 8);
  assert.deepEqual(snapshot.defaults, initial.defaults);
  for (const config of [
    { access_mode: "anonymous" },
    { ...record.config, endpoint: "https://user:secret@cobalt.example/" },
    { ...record.config, endpoint: "https://cobalt.example/?key=secret" },
    { ...record.config, endpoint: "https://cobalt.example:bad/" },
    { ...record.config, access_mode: "bearer" },
    { ...record.config, fallback_policy: "automatic" },
    { ...record.config, socket_timeout: 301 },
    { ...record.config, api_key: "never-copy-this" },
    { ...record.config, use_api_key: true },
    { ...record.config, credential_ref: "settings:cobalt_api_key" },
  ]) {
    await assert.rejects(api.updatePluginInstance(record.id, { config }, created.revision), (error) => {
      assert.equal(error.status, 422);
      assert.doesNotMatch(error.message, /secret|never-copy-this|settings:cobalt_api_key/);
      return true;
    });
  }
  const updated = await api.updatePluginInstance(record.id, { config: { ...record.config, endpoint: "https://cobalt.example/api", fallback_policy: "ytdlp" } }, created.revision);
  assert.equal(updated.instance.config.endpoint, "https://cobalt.example/api/");
  assert.equal(updated.instance.config.fallback_policy, "ytdlp");
  assert.doesNotMatch(JSON.stringify(updated.instance), /never-copy-this|credential_ref|use_api_key/);
  await assert.rejects(api.updatePluginDefaults({ download: record.id }, created.revision), (error) => error.status === 409);
  const selected = await api.updatePluginDefaults({ download: record.id }, updated.revision);
  assert.equal(selected.defaults.download, record.id);
  assert.equal(Object.keys(selected.defaults).length, 7);
});

test("Mock task creation records selected instances for URL and upload", async () => {
  values.clear();
  const snapshot = await Api.listPluginInstances();
  const download = snapshot.instances.find((instance) => instance.stage === "download");
  const payload = { url: "https://example.com/video", sourceLang: "en", targetLang: "zh-CN", mode: "mono", burn: "soft", model: "local:tiny", engine: "deepseek", needSubtitle: false, pluginInstances: { download: download.id } };
  const urlTask = await Api.createTask(payload);
  assert.deepEqual(urlTask.pluginInstances, { download: download.id });
  const uploadTask = await Api.createUploadTask({ ...payload, file: new File(["video"], "clip.mp4") });
  assert.deepEqual(uploadTask.pluginInstances, {});
});
