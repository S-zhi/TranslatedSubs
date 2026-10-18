import { test } from "node:test";
import assert from "node:assert/strict";

// Setup global mock window and global mock fetch before importing api.js
globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: false,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  }
};

let mockFetchHandler = null;
globalThis.fetch = async (url, options) => {
  if (mockFetchHandler) {
    return mockFetchHandler(url, options);
  }
  throw new Error("mockFetchHandler not set");
};

// Use dynamic import to prevent ESM hoisting from running the import before global setup
const { Api } = await import("../js/api.js");
const { resolveTaskWhisperState } = await import("../js/task-whisper.js");

test("Api.probeVideo sends only the URL by default", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video");
  assert.deepEqual(requestBody, { url: "https://example.com/video" });
});

test("Api.probeVideo sends only a whitelisted browser cookie source when selected", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video", {
    cookiesFromBrowser: "chrome",
    cookieFile: "/private/profile/cookies.txt",
    cookies: "secret",
  });
  assert.deepEqual(requestBody, {
    url: "https://example.com/video",
    cookiesFromBrowser: "chrome",
  });
});

test("Api.probeVideo drops an invalid browser cookie source", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video", { cookiesFromBrowser: "profile/path" });
  assert.deepEqual(requestBody, { url: "https://example.com/video" });
});

test("Api.probeVideo sends selected downloader and quality without unrelated stages", async () => {
  let requestBody;
  mockFetchHandler = async (_url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };
  await Api.probeVideo("https://example.com/video", {
    pluginInstances: { download: "alternate", transcribe: "unused" }, quality: "720p",
  });
  assert.deepEqual(requestBody, { url: "https://example.com/video", pluginInstances: { download: "alternate" }, quality: "720p" });
});

test("Api.getProbeStartupStatus reads the shared startup batch endpoint", async () => {
  let requestedUrl = "";
  mockFetchHandler = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ state: "running", total: 10 }) };
  };

  const status = await Api.getProbeStartupStatus();
  assert.equal(requestedUrl, "http://localhost:8000/api/tasks/probe/startup-status");
  assert.equal(status.total, 10);
});

test("Api.openFolder: JSON response with detail", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => JSON.stringify({ detail: "任务目录尚未生成" })
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：任务目录尚未生成/);
      return true;
    }
  );
});

test("Api.openFolder: structured detail includes actionable suggestion", async () => {
  mockFetchHandler = async () => ({
    ok: false,
    status: 413,
    text: async () => JSON.stringify({
      detail: {
        code: "UPLOAD_TOO_LARGE",
        message: "上传文件过大",
        suggestion: "请压缩或切分视频",
      },
    }),
  });

  await assert.rejects(
    () => Api.openFolder("some-id"),
    /打开文件夹失败：上传文件过大；请压缩或切分视频/,
  );
});

test("Api.cancelTask sends POST to /api/tasks/:id/cancel", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return {
      ok: true,
      json: async () => ({ id: "task_123", status: "CANCELLED", error: "用户取消" })
    };
  };

  const res = await Api.cancelTask("task_123");
  assert.equal(requestedUrl, "http://localhost:8000/api/tasks/task_123/cancel");
  assert.equal(requestedMethod, "POST");
  assert.equal(res.status, "CANCELLED");
});

test("Api.deleteLocalModel sends DELETE to the model resource", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return { ok: true, json: async () => ({ name: "large-v3", status: "NOT_INSTALLED" }) };
  };

  const model = await Api.deleteLocalModel("large-v3");
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/large-v3");
  assert.equal(requestedMethod, "DELETE");
  assert.equal(model.status, "NOT_INSTALLED");
});

test("Api.downloadLocalModel starts an official model download", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return { ok: true, json: async () => ({ name: "tiny.en", status: "DOWNLOADING" }) };
  };

  const model = await Api.downloadLocalModel("tiny.en");
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/tiny.en/download");
  assert.equal(requestedMethod, "POST");
  assert.equal(model.status, "DOWNLOADING");
});

test("Api.importLocalModel uploads provided files without requesting a download", async () => {
  let requestedUrl = "";
  let requestedBody;
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedBody = options.body;
    assert.equal(options.method, "POST");
    return { ok: true, json: async () => ({ name: "external", status: "READY" }) };
  };

  const file = new File(["weights"], "pytorch_model.bin");
  const result = await Api.importLocalModel("external", "External", [file]);
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/import");
  assert.equal(requestedBody.get("name"), "external");
  assert.equal(requestedBody.get("files").name, "pytorch_model.bin");
  assert.equal(result.status, "READY");
});

test("Api.subscribeProgress: handles message, end, timeout, and reconnecting", async () => {
  const instances = [];
  class MockEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.readyState = 0;
      instances.push(this);
    }
    addEventListener(event, fn) {
      this.listeners[event] = fn;
    }
    removeEventListener() {}
    close() {
      this.readyState = 2;
    }
    emit(event, data) {
      if (event === "message" && this.onmessage) {
        this.onmessage({ data: JSON.stringify(data) });
      } else if (this.listeners[event]) {
        this.listeners[event]({ data: JSON.stringify(data) });
      }
    }
    emitError() {
      if (this.onerror) this.onerror(new Event("error"));
    }
  }

  globalThis.EventSource = MockEventSource;

  const updates = [];
  const unsub = Api.subscribeProgress("task_test1", (data) => {
    updates.push(data);
  });

  assert.equal(instances.length, 1);
  const es1 = instances[0];

  // 1) 收到普通 progress 消息
  es1.emit("message", { id: "task_test1", status: "TRANSCRIBING", progress: 40 });
  assert.equal(updates.length, 1);
  assert.equal(updates[0]._streamStatus, "connected");
  assert.equal(updates[0].progress, 40);

  // 2) 连接异常 -> 触发 reconnecting 状态
  es1.emitError();
  assert.equal(updates.length, 2);
  assert.equal(updates[1]._streamStatus, "reconnecting");
  assert.equal(updates[1]._retryCount, 1);

  // 3) end 事件处理
  es1.emit("end", { id: "task_test1", status: "SUCCESS", progress: 100 });
  assert.equal(updates.length, 3);
  assert.equal(updates[2].status, "SUCCESS");

  // 4) timeout 事件处理
  es1.emit("timeout", { error: "stream timeout" });
  assert.equal(updates.length, 4);
  assert.equal(updates[3]._streamStatus, "timeout");

  unsub();
  delete globalThis.EventSource;
});

test("Api.openFolder: non-JSON response fallback to text content", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => "Internal Server Error"
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：Internal Server Error/);
      return true;
    }
  );
});

test("Api.openFolder: empty text response fallback to status code", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => ""
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：409/);
      return true;
    }
  );
});

test("structured API errors retain message, code, status and detail", async () => {
  mockFetchHandler = async () => ({
    ok: false,
    status: 409,
    text: async () => JSON.stringify({ detail: { code: "MODEL_CURRENT", message: "当前模型不能删除" } }),
  });
  await assert.rejects(Api.deleteLocalModel("tiny"), (error) => {
    assert.equal(error.code, "MODEL_CURRENT");
    assert.equal(error.status, 409);
    assert.deepEqual(error.detail, { code: "MODEL_CURRENT", message: "当前模型不能删除" });
    assert.match(error.message, /当前模型不能删除/);
    assert.match(error.message, /切换当前模型/);
    return true;
  });
});

test("model import dependency errors keep the server code and multipart metadata", async () => {
  let options;
  mockFetchHandler = async (_url, requestOptions) => {
    options = requestOptions;
    return {
      ok: false,
      status: 503,
      text: async () => JSON.stringify({ detail: { code: "MODEL_DEPENDENCY_MISSING", message: "请启用 HF 依赖" } }),
    };
  };
  await assert.rejects(Api.importLocalModel("external", "External", [new File(["weights"], "pytorch_model.bin")]), (error) => {
    assert.equal(error.code, "MODEL_DEPENDENCY_MISSING");
    assert.equal(error.status, 503);
    assert.match(error.message, /请启用 HF 依赖/);
    return true;
  });
  assert.equal(options.headers?.["Content-Type"], undefined, "browser must choose the multipart boundary");
  assert.equal(options.body.get("name"), "external");
  assert.equal(options.body.get("files").name, "pytorch_model.bin");
});

test("model settings updates filter read-only fields recursively but preserve API key input", async () => {
  let body;
  mockFetchHandler = async (_url, options) => {
    body = JSON.parse(options.body);
    return { ok: true, json: async () => ({}) };
  };
  await Api.updateModelSettings({
    llm: {
      enabled: true, apiKey: "new-secret", hasApiKey: true, runtimeManaged: true,
      _runtimeManaged: true, status: "READY", updatedAt: "read-only",
      models: [{ id: "llm-1", model: "chat", apiKey: "model-secret", hasApiKey: true, status: "READY", runtimeManaged: true, updatedAt: 1 }],
    },
  });
  assert.deepEqual(body, {
    llm: { enabled: true, apiKey: "new-secret", models: [{ id: "llm-1", model: "chat", apiKey: "model-secret" }] },
  });
});

test("task payload keeps the explicitly selected Whisper model", async () => {
  let submitted;
  mockFetchHandler = async (_url, options) => {
    submitted = JSON.parse(options.body);
    return { ok: true, json: async () => ({ id: "task_snapshot" }) };
  };
  await Api.createTask({ url: "https://example.com/v", model: "local:base", needSubtitle: true });
  assert.equal(submitted.model, "local:base");
});

test("Whisper task policy accepts camel and snake readiness contracts without fake models", () => {
  const items = [
    { name: "tiny", status: "READY" },
    { name: "base", status: "NOT_INSTALLED" },
    { name: "tiny", status: "READY" },
  ];
  const camel = resolveTaskWhisperState({
    items,
    settings: { whisper: { model: "local:tiny" } },
    readiness: { modelSettings: { whisper: { runtimeManaged: true, enabled: true, model: "local:tiny" } } },
  });
  assert.equal(camel.ok, true);
  assert.equal(camel.locked, true);
  assert.deepEqual(camel.options.map((item) => item.value), ["local:tiny"]);
  const snakeMissing = resolveTaskWhisperState({
    items,
    settings: { whisper: { model: "local:base" } },
    readiness: { model_settings: { whisper: { runtimeManaged: true, enabled: true, model: "local:base" } } },
  });
  assert.equal(snakeMissing.ok, false);
  assert.equal(snakeMissing.model, "local:base");
  assert.equal(resolveTaskWhisperState({ items: [], settings: {}, readiness: {} }).ok, false);
  const noInstalled = resolveTaskWhisperState({
    items: [{ name: "tiny", status: "NOT_INSTALLED" }, { name: "base", status: "ERROR" }],
    settings: { whisper: { runtimeManaged: false, model: "local:tiny" } },
    readiness: {},
  });
  assert.equal(noInstalled.ok, false);
  assert.deepEqual(noInstalled.options, []);
  assert.equal(noInstalled.model, undefined, "initial HTML tiny placeholder is not treated as a real model");
});

test("Whisper directory upload uses a long timeout and lets fetch create multipart boundary", async () => {
  let signal;
  let requestOptions;
  window.APP_CONFIG.API_TIMEOUT_MS = 1;
  mockFetchHandler = async (_url, options) => {
    signal = options.signal;
    requestOptions = options;
    await new Promise((resolve) => setTimeout(resolve, 25));
    return { ok: true, json: async () => ({ name: "external", status: "READY" }) };
  };
  await Api.importLocalModel("external", "External", [new File(["weights"], "pytorch_model.bin")]);
  window.APP_CONFIG.API_TIMEOUT_MS = 15000;
  assert.equal(signal.aborted, false);
  assert.equal(requestOptions.headers?.["Content-Type"], undefined);
});

test("mock Whisper inventory, validation, retry, current guard and task snapshots share one state", async () => {
  const memory = new Map();
  globalThis.localStorage = {
    getItem(key) { return memory.has(key) ? memory.get(key) : null; },
    setItem(key, value) { memory.set(key, String(value)); },
    removeItem(key) { memory.delete(key); },
  };
  window.APP_CONFIG.USE_MOCK = true;
  const { Api: MockApi } = await import("../js/api.js?mock-contract=whisper");
  try {
    const initial = await MockApi.listLocalModels();
    assert.equal(initial.length, 12);
    assert.deepEqual(initial.filter((item) => item.status === "READY").map((item) => item.name), ["tiny"]);

    const downloading = await MockApi.downloadLocalModel("base");
    assert.equal(downloading.status, "DOWNLOADING");
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal((await MockApi.listLocalModels()).find((item) => item.name === "base").status, "READY");

    const validFiles = [
      { name: "config.json", size: 20, text: async () => JSON.stringify({ model_type: "whisper" }) },
      { name: "preprocessor_config.json", size: 10 }, { name: "tokenizer.json", size: 10 },
      { name: "model.safetensors", size: 40 },
    ];
    const imported = await MockApi.importLocalModel("external-whisper", "保留标签", validFiles);
    assert.equal(imported.label, "保留标签");
    assert.equal(imported.status, "READY");
    assert.equal((await MockApi.getModelSettings()).whisper.model, "local:tiny", "import must not activate the model");
    await assert.rejects(MockApi.importLocalModel("bad-whisper", "Bad", [{ name: "weights.bin", size: 4 }]), (error) => error.code === "MODEL_INVALID");
    await assert.rejects(MockApi.importLocalModel("duplicate-whisper", "Duplicate", [...validFiles, validFiles[0]]), (error) => error.code === "MODEL_INVALID");
    memory.set("subtrans_mock_hf_dependency_enabled", "false");
    await assert.rejects(MockApi.importLocalModel("dependency-whisper", "Missing deps", validFiles), (error) => error.code === "MODEL_DEPENDENCY_MISSING");
    memory.delete("subtrans_mock_hf_dependency_enabled");
    const storedAssets = JSON.parse(memory.get("subtrans_mock_whisper_assets_v2"));
    const importedAsset = storedAssets.find((item) => item.name === "external-whisper");
    importedAsset.files = ["model.safetensors"];
    memory.set("subtrans_mock_whisper_assets_v2", JSON.stringify(storedAssets));
    await assert.rejects(MockApi.checkLocalModel("external-whisper"), (error) => error.code === "MODEL_INVALID");
    assert.equal((await MockApi.listLocalModels()).find((item) => item.name === "external-whisper").status, "ERROR");

    memory.set("subtrans_mock_whisper_fail_next_download", "1");
    await MockApi.downloadLocalModel("small");
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal((await MockApi.listLocalModels()).find((item) => item.name === "small").status, "ERROR");
    await MockApi.downloadLocalModel("small");
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal((await MockApi.listLocalModels()).find((item) => item.name === "small").status, "READY");

    let settings = await MockApi.getModelSettings();
    const tinyCard = settings.whisper.models.find((item) => item.model === "local:tiny");
    tinyCard.id = "stable-custom-card-id";
    settings = await MockApi.updateModelSettings({ whisper: { ...settings.whisper, model: "local:tiny", models: settings.whisper.models, hasApiKey: true, runtimeManaged: false } });
    assert.equal(settings.whisper.models.filter((item) => item.isCurrent).length, 1);
    assert.equal(settings.whisper.models.find((item) => item.model === "local:tiny").id, "stable-custom-card-id");
    assert.equal(settings.whisper.runtimeManaged, true, "only a successful save activates management");
    await assert.rejects(MockApi.updateModelSettings({ whisper: { ...settings.whisper, model: "local:medium" } }), (error) => error.code === "MODEL_NOT_READY");
    await assert.rejects(MockApi.deleteLocalModel("tiny"), (error) => error.code === "MODEL_CURRENT");

    const task = await MockApi.createTask({ url: "https://example.com/demo", model: "local:base", needSubtitle: true });
    assert.equal(task.model, "local:tiny", "managed effective config wins when creating task");
    assert.equal(task.runtimeConfig.whisper.model, "local:tiny");
    const nextCards = settings.whisper.models.map((item) => ({ ...item, isCurrent: item.model === "local:base" }));
    await MockApi.updateModelSettings({ whisper: { ...settings.whisper, model: "local:base", models: nextCards } });
    assert.equal((await MockApi.listTasks()).find((item) => item.id === task.id).runtimeConfig.whisper.model, "local:tiny", "task snapshot survives later settings changes");
    await assert.rejects(MockApi.deleteLocalModel("base"), (error) => error.code === "MODEL_CURRENT");
    await MockApi.updateModelSettings({ whisper: { ...settings.whisper, enabled: false, model: "local:base", models: nextCards } });
    const uploadTask = await MockApi.createUploadTask({ file: { name: "uploaded.mp4" }, model: "local:small", needSubtitle: true });
    assert.equal(uploadTask.model, "local:small");
    assert.equal(uploadTask.runtimeConfig.whisper.model, "local:small");
    await assert.rejects(MockApi.deleteLocalModel("small"), (error) => error.code === "MODEL_IN_USE");
    const deletedOfficial = await MockApi.deleteLocalModel("medium");
    assert.equal(deletedOfficial.status, "NOT_INSTALLED");
    assert.equal((await MockApi.listLocalModels()).filter((item) => item.source === "official").length, 12, "official catalog entry remains reinstallable");
    await MockApi.downloadLocalModel("medium");
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal((await MockApi.listLocalModels()).find((item) => item.name === "medium").status, "READY");
  } finally {
    window.APP_CONFIG.USE_MOCK = false;
    delete globalThis.localStorage;
  }
});

test("initConsole gates the initial form until live Whisper inventory is loaded", async (t) => {
  class NodeMock {
    constructor(value = "") {
      this.value = value; this.disabled = false; this.checked = false; this.hidden = false;
      this.id = ""; this.name = ""; this.dataset = {}; this.files = []; this.handlers = {};
      this.children = []; this.options = []; this.classList = { toggle() {}, add() {}, remove() {} };
    }
    set innerHTML(value) { this._html = value; if (value === "") { this.children = []; this.options = []; } }
    get innerHTML() { return this._html || ""; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    append(child) { this.children.push(child); this.options.push(child); }
    querySelector(selector) { return this.nodes?.get(selector) || null; }
    querySelectorAll(selector) {
      if (selector === ".param") return this.params || [];
      if (selector === "select, input") return this.controls || [];
      if (selector === 'input[name="needSubtitle"]') return [];
      return [];
    }
  }
  class DocumentMock {
    constructor() { this.nodes = new Map(); this.handlers = {}; this.body = { dataset: { route: "tasks" } }; }
    querySelector(selector) { return this.nodes.get(selector) || null; }
    createElement() { return new NodeMock(); }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  }
  const doc = new DocumentMock();
  globalThis.document = doc;
  const make = (selector, value = "") => { const node = new NodeMock(value); doc.nodes.set(selector, node); return node; };
  const ids = ["#engine", "#sourceLang", "#targetLang", "#model", "#whisperModelHint", "#url", "#videoFile", "#uploadBtn", "#urlHint", "#consoleBar", "#submitBtn", "#quality", "#toast", "#ttsHint"];
  const nodes = Object.fromEntries(ids.map((selector) => [selector, make(selector, selector === "#model" ? "local:tiny" : "")]));
  const needSubtitle = new NodeMock("on");
  const mode = new NodeMock("mono");
  const ttsEnabled = new NodeMock();
  const ttsVoice = new NodeMock("auto");
  const originalVoiceMode = new NodeMock("keep");
  const form = make("#taskForm");
  form.elements = { needSubtitle, mode, ttsEnabled, ttsVoice, originalVoiceMode };
  form.handlers = {};
  form.addEventListener = NodeMock.prototype.addEventListener;
  const params = new NodeMock();
  params.controls = [nodes["#sourceLang"], nodes["#targetLang"], nodes["#model"], nodes["#engine"]];
  nodes["#modelParam"] = params;
  form.querySelector = (selector) => selector === ".params" ? params : null;
  form.querySelectorAll = () => [];
  params.params = [params];
  params.querySelector = () => null;
  nodes["#model"].id = "model";
  nodes["#sourceLang"].id = "sourceLang";
  nodes["#targetLang"].id = "targetLang";
  nodes["#engine"].id = "engine";
  const submit = nodes["#submitBtn"];

  t.mock.method(Api, "listTranslationEngines", async () => []);
  t.mock.method(Api, "listVideoLanguages", async () => ["en"]);
  t.mock.method(Api, "listTargetLanguages", async () => ["zh-CN"]);
  t.mock.method(Api, "listLocalModels", async () => [{ name: "tiny", label: "Whisper tiny", status: "READY" }]);
  t.mock.method(Api, "getModelSettings", async () => ({ whisper: { runtimeManaged: true, enabled: true, model: "local:tiny" } }));
  t.mock.method(Api, "getRuntimeReadiness", async () => { throw new Error("must not fetch readiness when public runtimeManaged exists"); });
  const { initConsole } = await import("../js/ui-console.js?init-console-dom-test");
  initConsole();
  assert.equal(submit.disabled, true, "unresolved inventory closes the initial submit gate");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nodes["#model"].value, "local:tiny");
  assert.equal(nodes["#model"].disabled, true, "managed model stays locked after form parameter synchronization");
  assert.equal(submit.disabled, false, "ready live inventory opens the gate");
});
