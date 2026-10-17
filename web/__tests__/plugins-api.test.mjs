import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = { APP_CONFIG: { USE_MOCK: false, API_BASE_URL: "http://localhost:8000", API_TIMEOUT_MS: 15000 } };
const { Api } = await import("../js/api.js");
const calls = [];
globalThis.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ revision: 8, instance: { id: "custom" } }) }; };

test("plugin API reads catalog, list, encoded detail and writes CAS headers on every mutation", async () => {
  calls.length = 0;
  await Api.getPluginCatalog();
  await Api.listPluginInstances();
  await Api.getPluginInstance("my instance");
  await Api.createPluginInstance({ id: "new", config: { threads: 2 } }, 7);
  await Api.updatePluginInstance("my instance", { enabled: false }, 8);
  await Api.deletePluginInstance("my instance", 9);
  await Api.updatePluginDefaults({ translate: "new" }, 10);
  assert.deepEqual(calls.map((call) => call.url.replace("http://localhost:8000", "")), [
    "/api/settings/plugins/catalog", "/api/settings/plugins/instances", "/api/settings/plugins/instances/my%20instance",
    "/api/settings/plugins/instances", "/api/settings/plugins/instances/my%20instance", "/api/settings/plugins/instances/my%20instance", "/api/settings/plugins/defaults",
  ]);
  assert.deepEqual(calls.slice(3).map((call) => [call.options.method, call.options.headers["If-Match"]]), [["POST", "7"], ["PUT", "8"], ["DELETE", "9"], ["PUT", "10"]]);
  assert.deepEqual(JSON.parse(calls[6].options.body), { defaults: { translate: "new" } });
  await assert.rejects(Api.createPluginInstance({}, undefined), /刷新插件设置/);
  assert.equal(calls.length, 7);
});

test("JSON and multipart task creation preserve stage-to-instance references", async () => {
  calls.length = 0;
  const pluginInstances = { download: "download-hq", translate: "translator-1" };
  const payload = { sourceLang: "en", targetLang: "zh-CN", mode: "mono", burn: "hard", model: "local:tiny", engine: "deepseek", needSubtitle: true, ttsEnabled: false, pluginInstances };
  await Api.createTask({ ...payload, url: "https://example.com/video" });
  await Api.createUploadTask({ ...payload, file: new File(["video"], "clip.mp4", { type: "video/mp4" }) });
  assert.deepEqual(JSON.parse(calls[0].options.body).pluginInstances, pluginInstances);
  assert.deepEqual(JSON.parse(calls[1].options.body.get("pluginInstances")), pluginInstances);
  assert.equal(calls[1].options.headers, undefined);
});

test("plugin errors retain revision conflict, validation and retryable service status", async () => {
  const cases = [[409, { code: "REVISION_CONFLICT", message: "版本冲突" }], [422, [{ loc: ["body", "config", "threads"], msg: "范围错误" }]], [503, { message: "Redis unavailable" }]];
  for (const [status, detail] of cases) {
    globalThis.fetch = async () => ({ ok: false, status, text: async () => JSON.stringify({ detail }) });
    await assert.rejects(Api.updatePluginInstance("x", {}, 1), (error) => error.status === status && JSON.stringify(error.detail) === JSON.stringify(detail));
  }
});
