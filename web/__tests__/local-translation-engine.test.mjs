import { test } from "node:test";
import assert from "node:assert/strict";

const values = new Map();
const localStorage = {
  getItem(key) { return values.get(key) ?? null; },
  setItem(key, value) { values.set(key, String(value)); },
};
globalThis.localStorage = localStorage;
globalThis.window = {
  APP_CONFIG: { USE_MOCK: true, API_BASE_URL: "http://localhost:8000", API_TIMEOUT_MS: 15000 },
  localStorage,
};

const { Api, USE_MOCK } = await import("../js/api.js");
const {
  dispatchLocalEngineStateChange,
  formatInstalledModelSize,
  localEngineCardTemplate,
  localModelStatusView,
} = await import("../js/ui-translation-settings.js");
const {
  collectTaskPayload,
  createEngineLanguageController,
  isTaskEngineSelectable,
  localTranslationLanguagePair,
  preferredTaskEngine,
  requiresReadyLocalTranslation,
  ttsHintForTarget,
} = await import("../js/ui-console.js");

test("local engine settings card hides API credentials and reports install actions", () => {
  const card = localEngineCardTemplate({ id: "local-opus-en-zh", modelStatus: "NOT_INSTALLED", enabled: true });

  assert.match(card, /英语 → 简体中文/);
  assert.match(card, /CPU 推理/);
  assert.match(card, /下载并转换/);
  assert.match(card, /离线检测/);
  assert.doesNotMatch(card, /data-field="apiKey"|Base URL|API Key/);
  assert.deepEqual(localModelStatusView("CONVERTING"), ["busy", "转换中"]);
  assert.equal(formatInstalledModelSize(2 * 1024 ** 3), "2.0 GB");
});

test("a READY refresh enables the local option and notifies task settings only on state changes", async () => {
  assert.equal(USE_MOCK, true);
  const initial = await Api.listTranslationEngines();
  const notReady = initial.find((engine) => engine.id === "local-opus-en-zh");
  assert.equal(notReady.modelStatus, "NOT_INSTALLED");
  assert.equal(isTaskEngineSelectable(notReady), false);
  const cloudAlongside = [{ id: "deepseek", enabled: true, availability: "AVAILABLE" }, notReady];
  assert.equal(preferredTaskEngine(cloudAlongside, "").id, "local-opus-en-zh");
  assert.equal(preferredTaskEngine([{ id: "deepseek", enabled: true, availability: "AVAILABLE" }], "").id, "deepseek");

  await Api.downloadTranslationEngine("local-opus-en-zh");
  const refreshed = await Api.listTranslationEngines();
  const ready = refreshed.find((engine) => engine.id === "local-opus-en-zh");
  assert.equal(ready.modelStatus, "READY");
  assert.equal(ready.hasApiKey, false);
  assert.equal(isTaskEngineSelectable(ready), true);
  let notifications = 0;
  assert.equal(dispatchLocalEngineStateChange(notReady, ready, () => { notifications += 1; }), true);
  assert.equal(dispatchLocalEngineStateChange(ready, { ...ready }, () => { notifications += 1; }), false);
  assert.equal(notifications, 1);
  const withCloud = [{ id: "deepseek", enabled: true, availability: "AVAILABLE", active: true }, ...refreshed];
  assert.equal(preferredTaskEngine(withCloud, "local-opus-en-zh").id, "local-opus-en-zh");
  assert.equal(preferredTaskEngine(withCloud, "").id, "local-opus-en-zh");
  assert.equal(preferredTaskEngine(withCloud, "deepseek").id, "deepseek");
  assert.deepEqual(localTranslationLanguagePair(), { sourceLang: "en", targetLang: "zh-CN" });
});

test("selecting local locks its language pair, then restores languages and TTS guidance on cloud switch", () => {
  const engineSelect = { value: "deepseek" };
  const sourceSelect = { value: "de", disabled: false };
  const targetSelect = { value: "ja", disabled: false };
  let hint = ttsHintForTarget(targetSelect.value, true);
  const controller = createEngineLanguageController({
    engineSelect,
    sourceSelect,
    targetSelect,
    onLockChange(locked) {
      sourceSelect.disabled = locked;
      targetSelect.disabled = locked;
      hint = ttsHintForTarget(targetSelect.value, true);
    },
  });
  controller.setEngines([
    { id: "deepseek", enabled: true, availability: "AVAILABLE" },
    { id: "local-opus-en-zh", enabled: true, modelStatus: "READY" },
  ]);

  engineSelect.value = "local-opus-en-zh";
  assert.equal(controller.sync(), true);
  assert.deepEqual([sourceSelect.value, targetSelect.value], ["en", "zh-CN"]);
  assert.equal(sourceSelect.disabled, true);
  assert.equal(targetSelect.disabled, true);
  assert.match(hint, /配音视频/);

  engineSelect.value = "deepseek";
  assert.equal(controller.sync(), false);
  assert.deepEqual([sourceSelect.value, targetSelect.value], ["de", "ja"]);
  assert.equal(sourceSelect.disabled, false);
  assert.equal(targetSelect.disabled, false);
  assert.match(hint, /不支持 Kokoro 配音/);
});

test("task payload reads selected values even while language selects are disabled", () => {
  const fields = {
    "#sourceLang": { value: "en", disabled: true },
    "#targetLang": { value: "zh-CN", disabled: true },
    "#model": { value: "replicate:small.en" },
    "#engine": { value: "local-opus-en-zh" },
    "#quality": { value: "720p" },
  };
  globalThis.document = { querySelector(selector) { return fields[selector] || null; } };
  const form = { elements: {
    mode: { value: "hard" },
    burn: { value: "hard" },
    needSubtitle: { value: "on" },
    ttsEnabled: { checked: true },
    ttsVoice: { value: "af_heart" },
    originalVoiceMode: { value: "keep" },
  } };

  assert.deepEqual(collectTaskPayload(form), {
    sourceLang: "en",
    targetLang: "zh-CN",
    mode: "hard",
    burn: "hard",
    model: "replicate:small.en",
    engine: "local-opus-en-zh",
    needSubtitle: true,
    quality: "720p",
    ttsEnabled: true,
    ttsVoice: "af_heart",
    originalVoiceMode: "keep",
  });
});

test("an uninstalled local model blocks subtitle translation but permits download-only tasks", () => {
  const local = { id: "local-opus-en-zh", modelStatus: "NOT_INSTALLED" };
  assert.equal(requiresReadyLocalTranslation(local, true), true);
  assert.equal(requiresReadyLocalTranslation(local, false), false);
  assert.equal(requiresReadyLocalTranslation({ ...local, modelStatus: "READY" }, true), false);
});
