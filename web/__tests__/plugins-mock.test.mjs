import { test } from "node:test";
import assert from "node:assert/strict";

const values = new Map();
globalThis.localStorage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
globalThis.window = { localStorage, APP_CONFIG: { USE_MOCK: true } };
const { Api } = await import("../js/api.js");
const { createMockPluginApi } = await import("../js/mock-plugins.js");

test("MockAPI exposes seven catalog stages, CAS CRUD and atomic defaults patch without credentials", async () => {
  values.clear();
  const { plugins } = await Api.getPluginCatalog();
  assert.deepEqual(plugins.map((plugin) => plugin.stage), ["download", "audio_extract", "vocal_separate", "transcribe", "translate", "tts", "render"]);
  const original = await Api.listPluginInstances();
  assert.equal(Object.keys(original.defaults).length, 7);
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
