/* 任务控制台：参数初始化 + URL 校验 + 提交 */

import { $, el } from "./utils.js";
import { createTask } from "./store.js";
import { toast } from "./toast.js";
import { Api } from "./api.js";
import { LANG_LABEL } from "./constants.js";
import { resolveTaskWhisperState } from "./task-whisper.js";
import { collectTaskPluginInstances, createTaskPluginController } from "./task-plugins.js";

const CFG = window.APP_CONFIG;
const FALLBACK_LANGUAGES = [
  "en", "zh", "hi", "es", "ar", "fr", "pt", "ru",
  "de", "ko", "ja",
];
const UNSUPPORTED_LANGUAGES = new Set(["bn", "ur"]);
const FALLBACK_TARGET_LANGUAGES = Object.keys(LANG_LABEL).filter(
  (k) => k !== "auto" && k !== "zh" && !UNSUPPORTED_LANGUAGES.has(k)
);
const DEFAULT_SOURCE_LANGUAGE = "en";
const URL_RE = /^https?:\/\/.+/i;
const VIDEO_EXT_RE = /\.(mp4|mov|mkv|webm|avi|m4v|flv|ts|mpeg|mpg|wmv)$/i;
const PROBE_DEBOUNCE_MS = 800;
const LANGUAGE_DISPLAY = typeof Intl !== "undefined" && Intl.DisplayNames
  ? new Intl.DisplayNames(["zh-CN"], { type: "language" })
  : null;
const LOCAL_ENGINE_ID = "local-opus-en-zh";
const LOCAL_ENGINE_FALLBACK = {
  id: LOCAL_ENGINE_ID,
  name: "本地 CPU 英译中",
  apiType: "local_ct2",
  model: "Helsinki-NLP/opus-mt-en-zh",
  enabled: true,
  availability: "UNAVAILABLE",
  modelStatus: "NOT_INSTALLED",
  hasApiKey: false,
};
let engineLanguageController = null;
let taskPluginController = null;
let taskWhisperState = null;
let whisperRefreshSeq = 0;
let lastWhisperSelection = "";
let whisperRefreshPromise = null;
let whisperRefreshDirty = true;
let whisperRefreshScheduled = false;
let taskSubmitSync = () => {};

export function localTranslationLanguagePair() {
  return { sourceLang: "en", targetLang: "zh-CN" };
}

export function ttsHintForTarget(targetLang, enabled) {
  if (!enabled) return "仅支持中文或英文目标语。";
  const supported = String(targetLang || "").toLowerCase().startsWith("zh")
    || String(targetLang || "").toLowerCase().startsWith("en");
  return supported
    ? "将生成单独的配音视频；原字幕视频仍会保留。"
    : "当前目标语不支持 Kokoro 配音，请改选中文或英文。";
}

export function isTaskEngineSelectable(engine) {
  if (!engine?.enabled) return false;
  if (engine.id === LOCAL_ENGINE_ID) return String(engine.modelStatus || "").toUpperCase() === "READY";
  return engine.id === "deepseek" || !engine.availability || engine.availability === "AVAILABLE";
}

export function requiresReadyLocalTranslation(engine, needSubtitle) {
  return Boolean(needSubtitle
    && engine?.id === LOCAL_ENGINE_ID
    && String(engine.modelStatus || "").toUpperCase() !== "READY");
}

export function preferredTaskEngine(items, currentValue = "") {
  const current = items.find((engine) => (engine.id || engine.value) === currentValue);
  if (current && (isTaskEngineSelectable(current) || current.id === LOCAL_ENGINE_ID)) return current;
  const local = items.find((engine) => engine.id === LOCAL_ENGINE_ID);
  if (local) return local;
  const selectable = items.filter(isTaskEngineSelectable);
  return selectable.find((engine) => engine.active) || selectable[0] || items[0] || null;
}

export function createEngineLanguageController({ engineSelect, sourceSelect, targetSelect, onLockChange = () => {} }) {
  let engines = [];
  let previous = null;
  let locked = false;

  function sync() {
    const selected = engines.find((engine) => engine.id === engineSelect.value);
    const shouldLock = selected?.id === LOCAL_ENGINE_ID
      && String(selected.modelStatus || "").toUpperCase() === "READY"
      && selected.enabled !== false;
    if (shouldLock && !locked) previous = { sourceLang: sourceSelect.value, targetLang: targetSelect.value };
    if (!shouldLock && locked && previous) {
      sourceSelect.value = previous.sourceLang;
      targetSelect.value = previous.targetLang;
      previous = null;
    }
    locked = shouldLock;
    if (locked) {
      const pair = localTranslationLanguagePair();
      sourceSelect.value = pair.sourceLang;
      targetSelect.value = pair.targetLang;
    }
    onLockChange(locked);
    return locked;
  }

  return {
    setEngines(items) { engines = items || []; return sync(); },
    sync,
    setOnLockChange(callback) { onLockChange = callback || (() => {}); onLockChange(locked); },
    isLocked() { return locked; },
    selectedEngine() { return engines.find((engine) => engine.id === engineSelect.value) || null; },
  };
}

export function collectTaskPayload(form) {
  return {
    sourceLang: $("#sourceLang").value,
    targetLang: $("#targetLang").value,
    mode: form.elements.mode.value,
    burn: form.elements.burn.value,
    model: $("#model").value,
    engine: $("#engine").value,
    needSubtitle: form.elements.needSubtitle.value === "on",
    quality: $("#quality")?.value || "480p",
    ttsEnabled: form.elements.ttsEnabled.checked,
    ttsVoice: form.elements.ttsVoice.value,
    originalVoiceMode: form.elements.originalVoiceMode.value,
    pluginInstances: collectTaskPluginInstances(form),
  };
}

function normalizeTaskEngines(engines) {
  const items = [...(engines || [])];
  if (!items.some((engine) => engine.id === LOCAL_ENGINE_ID)) items.unshift({ ...LOCAL_ENGINE_FALLBACK });
  return items.sort((left, right) => (left.id === LOCAL_ENGINE_ID ? -1 : right.id === LOCAL_ENGINE_ID ? 1 : 0));
}

function initEngines(engines = null) {
  // 初始化翻译引擎下拉框；引擎来自高级设置中的持久化配置。
  const sel = $("#engine");
  const previousValue = sel.value;
  sel.innerHTML = "";
  const items = normalizeTaskEngines(engines?.length ? engines : [LOCAL_ENGINE_FALLBACK]);
  items.forEach((e) => {
    const o = el("option");
    o.value = e.id || e.value;
    const isLocal = e.id === LOCAL_ENGINE_ID;
    const localReady = String(e.modelStatus || "").toUpperCase() === "READY";
    const unavailable = !isTaskEngineSelectable(e);
    const localState = !e.enabled ? "已停用" : !localReady ? (String(e.modelStatus || "").toUpperCase() === "FAILED" ? "模型失败" : "请先下载模型") : "";
    o.textContent = unavailable ? `${e.name || e.label}（${isLocal ? localState : e.availability === "UNCONFIGURED" ? "未配置" : "不可用"}）` : (e.name || e.label);
    o.disabled = unavailable;
    sel.append(o);
  });
  const selected = preferredTaskEngine(items, previousValue);
  if (selected) sel.value = selected.id || selected.value;
}

async function loadEngines() {
  try {
    // 检测由高级设置模块在应用启动时统一执行，任务页只读取最新状态。
    const items = normalizeTaskEngines(await Api.listTranslationEngines());
    initEngines(items);
    engineLanguageController?.setEngines(items);
  } catch (_) {
    initEngines();
    engineLanguageController?.setEngines([LOCAL_ENGINE_FALLBACK]);
  }
  taskPluginController?.applyControls();
  engineLanguageController?.sync();
}

function option(value, label = value) {
  // 构造一个 select option 元素。
  const item = el("option");
  item.value = value;
  item.textContent = label;
  return item;
}

function languageLabel(code) {
  // 把语言代码转换为中文展示文案，提交时仍使用原始代码。
  if (LANG_LABEL[code]) return LANG_LABEL[code];
  try {
    return LANGUAGE_DISPLAY?.of(code) || code;
  } catch (e) {
    return code;
  }
}

function isValidUrl(url) {
  // 判断输入是否是后端可接受的 http(s) 页面地址。
  return URL_RE.test(url);
}

function isVideoFile(file) {
  // 判断拖入或选择的文件是否属于后端支持的视频类型。
  return !!file && (file.type.startsWith("video/") || VIDEO_EXT_RE.test(file.name || ""));
}

function probeOkMessage(result) {
  // 根据探针成功结果生成控制台提示文案。
  return result.title
    ? `链接可下载：${result.title}`
    : "链接可下载，可以开始处理";
}

function probeFailMessage(result) {
  // 根据探针失败结果生成控制台提示文案。
  return result.reason || result.detail || "这个链接暂时无法下载";
}

function renderSourceLanguages(languages) {
  // 渲染源语言下拉框，保留自动检测作为本地特殊选项。
  const sel = $("#sourceLang");
  const current = sel.value || DEFAULT_SOURCE_LANGUAGE;
  sel.innerHTML = "";
  sel.append(option("auto", "自动检测"));
  languages.filter((code) => !UNSUPPORTED_LANGUAGES.has(code)).forEach((code) => {
    sel.append(option(code, languageLabel(code)));
  });
  sel.value = [...sel.options].some((item) => item.value === current)
    ? current
    : DEFAULT_SOURCE_LANGUAGE;
}

function renderTargetLanguages(languages) {
  // 渲染目标语言下拉框。
  const sel = $("#targetLang");
  const current = sel.value || "zh-CN";
  const available = languages.filter((code) => !UNSUPPORTED_LANGUAGES.has(code));
  sel.innerHTML = "";
  available.forEach((code) => {
    sel.append(option(code, languageLabel(code)));
  });
  sel.value = [...sel.options].some((item) => item.value === current)
    ? current
    : (available[0] || "zh-CN");
}

function modelLabel(model) {
  // 生成 Whisper 模型下拉框展示文案。
  const [backend, rawModel] = String(model).includes(":") ? String(model).split(":", 2) : ["local", model];
  const labels = {
    "tiny.en": "tiny.en · 英语最快",
    tiny: "tiny · 最快",
    "base.en": "base.en · 英语快",
    base: "base · 快",
    "small.en": "small.en · 英语推荐",
    small: "small · 推荐",
    "medium.en": "medium.en · 英语较准",
    medium: "medium · 较准",
    "large-v1": "large-v1 · 高精度",
    "large-v2": "large-v2 · 高精度",
  };
  const label = labels[rawModel] || rawModel;
  return `${backend === "local" ? "本地" : backend} · ${label}`;
}

function renderModelWeights(state) {
  // 任务页只显示库存中的 READY 模型；托管配置始终服从运行时有效配置。
  const sel = $("#model");
  if (!sel) return;
  const current = sel.value || lastWhisperSelection;
  sel.innerHTML = "";
  (state?.options || []).forEach((model) => sel.append(option(model.value, model.label || modelLabel(model.value))));
  let renderedState = state;
  const explicitlyMissing = !state?.locked && lastWhisperSelection === current && current
    && !(state?.options || []).some((item) => item.value === current);
  const missingModel = explicitlyMissing ? current : state?.model;
  if (missingModel && ![...sel.options].some((item) => item.value === missingModel)) {
    const missing = option(missingModel, `${modelLabel(missingModel)}（未就绪或不存在）`);
    missing.disabled = true;
    sel.append(missing);
  }
  const preferred = state?.locked ? state.model
    : explicitlyMissing ? current
      : ([...sel.options].some((item) => item.value === current) ? current : (state?.model || ""));
  sel.value = preferred;
  if (explicitlyMissing) renderedState = { ...state, ok: false, message: "所选 Whisper 模型已不可用，请在模型设置中修复。" };
  else if (preferred && !state?.locked) lastWhisperSelection = preferred;
  sel.disabled = !renderedState?.ok || renderedState?.locked;
  const hint = $("#whisperModelHint");
  if (hint) {
    hint.textContent = renderedState?.message || "";
    hint.classList.toggle("is-error", !renderedState?.ok);
  }
  taskWhisperState = renderedState;
  taskPluginController?.applyControls();
  taskSubmitSync();
}

function taskViewIsVisible() {
  return !document.body?.dataset?.route || document.body.dataset.route === "tasks";
}

function refreshTaskWhisperState({ force = false } = {}) {
  if (whisperRefreshPromise) {
    if (force) return whisperRefreshPromise.then(() => refreshTaskWhisperState({ force: true }));
    whisperRefreshDirty = true;
    return whisperRefreshPromise;
  }
  whisperRefreshDirty = true;
  if (!force && !taskViewIsVisible()) {
    whisperRefreshSeq += 1;
    return Promise.resolve(taskWhisperState);
  }
  const run = async () => {
    let result = taskWhisperState;
    while (whisperRefreshDirty && (force || taskViewIsVisible())) {
      force = false;
      whisperRefreshDirty = false;
      const sequence = ++whisperRefreshSeq;
      try {
        const [items, settings] = await Promise.all([Api.listLocalModels(), Api.getModelSettings()]);
        let readiness = null;
        if (typeof settings?.whisper?.runtimeManaged !== "boolean") {
          readiness = await Api.getRuntimeReadiness();
        }
        if (sequence !== whisperRefreshSeq) continue;
        result = resolveTaskWhisperState({ items, settings, readiness });
      } catch (error) {
        if (sequence !== whisperRefreshSeq) continue;
        result = { ok: false, options: [], message: error.message || "读取 Whisper 模型状态失败，请检查服务连接。" };
      }
      if (sequence === whisperRefreshSeq) renderModelWeights(result);
    }
    return result;
  };
  whisperRefreshPromise = run().finally(() => {
    whisperRefreshPromise = null;
    if (whisperRefreshDirty && taskViewIsVisible()) queueTaskWhisperRefresh();
  });
  return whisperRefreshPromise;
}

function queueTaskWhisperRefresh() {
  whisperRefreshDirty = true;
  whisperRefreshSeq += 1;
  if (!taskViewIsVisible() || whisperRefreshScheduled) return;
  whisperRefreshScheduled = true;
  queueMicrotask(() => {
    whisperRefreshScheduled = false;
    refreshTaskWhisperState();
  });
}

async function initSrtOptions() {
  // 语言选项可以本地回退；模型状态必须来自实时库存和有效配置。
  renderSourceLanguages(FALLBACK_LANGUAGES);
  renderTargetLanguages(CFG.TARGET_LANGUAGES || FALLBACK_TARGET_LANGUAGES);
  renderModelWeights({ ok: false, options: [], message: "正在读取 Whisper 模型状态…" });
  const languageResults = await Promise.allSettled([Api.listVideoLanguages(), Api.listTargetLanguages()]);
  if (languageResults[0].status === "fulfilled") renderSourceLanguages(languageResults[0].value);
  if (languageResults[1].status === "fulfilled") renderTargetLanguages(languageResults[1].value);
  if (languageResults.some((result) => result.status === "rejected")) {
    toast("部分语言选项读取失败，已保留语言默认值", "ph-warning-circle");
  }
  await refreshTaskWhisperState();
  engineLanguageController?.sync();
}

export function initConsole() {
  // 初始化控制台表单交互。
  engineLanguageController = createEngineLanguageController({
    engineSelect: $("#engine"),
    sourceSelect: $("#sourceLang"),
    targetSelect: $("#targetLang"),
  });
  initEngines();
  engineLanguageController.setEngines([LOCAL_ENGINE_FALLBACK]);
  document.addEventListener("translation-engines-change", () => loadEngines());
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "tasks") {
      loadEngines();
      refreshTaskWhisperState({ force: true });
    }
  });
  document.addEventListener("whisper-models-change", queueTaskWhisperRefresh);
  document.addEventListener("model-settings-change", queueTaskWhisperRefresh);
  initSrtOptions();

  const form = $("#taskForm");
  const urlInput = $("#url");
  const fileInput = $("#videoFile");
  const uploadBtn = $("#uploadBtn");
  const hint = $("#urlHint");
  const bar = $("#consoleBar");
  const submitBtn = $("#submitBtn");
  let probeTimer = null;
  let probeSeq = 0;
  let submitting = false;
  let selectedFile = null;
  let dragDepth = 0;
  const probeState = { status: "idle", url: "", key: "", result: null };

  function syncSubmitDisabled() {
    // 有字幕时必须确认最新 Whisper 模型状态后才允许提交。
    const needsSubtitle = form.elements.needSubtitle.value === "on";
    submitBtn.disabled = submitting
      || probeState.status === "checking"
      || probeState.status === "failed"
      || (taskPluginController && !taskPluginController.isReady())
      || (needsSubtitle && !(taskPluginController ? taskPluginController.whisperReady() : taskWhisperState?.ok));
  }
  taskSubmitSync = syncSubmitDisabled;
  syncSubmitDisabled();

  function clearProbeTimer() {
    // 清理尚未触发的自动探测定时器。
    if (probeTimer) clearTimeout(probeTimer);
    probeTimer = null;
  }

  function setUrlHint(message, isError = false) {
    // 更新 URL 提示区域及错误样式。
    bar.classList.toggle("is-error", isError);
    hint.classList.toggle("is-error", isError);
    hint.textContent = message;
  }

  function setProbeState(status, message, isError = false, result = null) {
    // 更新当前探针状态并刷新按钮与提示。
    probeState.status = status;
    probeState.result = result;
    setUrlHint(message, isError);
    syncSubmitDisabled();
  }

  function resetProbeState(message = "粘贴单个视频页面地址，或把本地视频拖进输入框") {
    // 重置探针状态到空闲。
    clearProbeTimer();
    probeSeq += 1;
    probeState.status = "idle";
    probeState.url = "";
    probeState.key = "";
    probeState.result = null;
    setUrlHint(message, false);
    syncSubmitDisabled();
  }

  function clearSelectedFile() {
    // 清空已选择的本地视频，恢复 URL 输入模式。
    selectedFile = null;
    fileInput.value = "";
    urlInput.dataset.sourceType = "url";
    urlInput.placeholder = "粘贴视频页面地址，或拖入本地视频";
    taskPluginController?.sync();
  }

  function setSelectedFile(file) {
    // 记录本地视频并切换到上传模式。
    selectedFile = file;
    clearProbeTimer();
    probeSeq += 1;
    probeState.status = "idle";
    probeState.url = "";
    probeState.key = "";
    probeState.result = null;
    urlInput.dataset.sourceType = "upload";
    urlInput.value = file.name;
    urlInput.placeholder = "已选择本地视频";
    taskPluginController?.sync();
    setUrlHint(`已选择本地视频：${file.name}，会跳过在线下载并按当前参数继续处理`, false);
    syncSubmitDisabled();
  }

  function currentProbeOptions() {
    const download = collectTaskPluginInstances(form).download;
    return {
      ...(download ? { pluginInstances: { download } } : {}),
      quality: $("#quality").value || "480p",
    };
  }

  function currentProbeKey(options = currentProbeOptions()) {
    return JSON.stringify([options, taskPluginController?.downloadFingerprint?.() || ""]);
  }

  async function runProbe(url, options = currentProbeOptions()) {
    // 对当前 URL 和下载配置执行探测，并忽略过期返回。
    const seq = ++probeSeq;
    const key = currentProbeKey(options);
    probeState.url = url;
    probeState.key = key;
    setProbeState("checking", "正在检查链接是否可下载...");
    try {
      const result = await Api.probeVideo(url, options);
      if (seq !== probeSeq || key !== currentProbeKey()) return { ok: false, stale: true };
      probeState.url = url;
      if (result.ok) {
        setProbeState("ok", probeOkMessage(result), false, result);
      } else {
        setProbeState("failed", probeFailMessage(result), true, result);
      }
      return result;
    } catch (err) {
      if (seq !== probeSeq || key !== currentProbeKey()) return { ok: false, stale: true };
      const result = {
        ok: false,
        reason: err.message || "链接校验失败",
      };
      setProbeState("failed", probeFailMessage(result), true, result);
      return result;
    }
  }

  function scheduleProbe() {
    // URL 输入变化后延迟触发探针，避免每次按键都请求后端。
    if (currentVideoFile()) {
      clearProbeTimer();
      probeSeq += 1;
      probeState.status = "idle";
      probeState.url = "";
      probeState.key = "";
      probeState.result = null;
      setUrlHint(`已选择本地视频：${currentVideoFile().name}，会跳过在线下载并按当前参数继续处理`, false);
      syncSubmitDisabled();
      return;
    }
    const url = urlInput.value.trim();
    clearProbeTimer();
    probeSeq += 1;
    probeState.key = currentProbeKey();
    if (!url) {
      resetProbeState();
      return;
    }
    if (!isValidUrl(url)) {
      probeSeq += 1;
      probeState.url = url;
      setProbeState("failed", "请输入有效的视频链接（以 http(s):// 开头）", true);
      return;
    }
    probeState.url = url;
    setProbeState("checking", "等待链接输入完成后自动检查...");
    probeTimer = setTimeout(() => runProbe(url), PROBE_DEBOUNCE_MS);
  }

  async function ensureProbeOk(url) {
    // 提交前确保当前 URL 已通过探针，防止绕过自动校验。
    clearProbeTimer();
    if (probeState.url === url && probeState.key === currentProbeKey() && probeState.status === "ok" && probeState.result?.ok) {
      return probeState.result;
    }
    return runProbe(url);
  }

  function currentVideoFile() {
    // 读取当前上传视频；即使组件状态被重建，也以 file input 中的文件为准。
    const file = selectedFile || Array.from(fileInput.files || []).find(isVideoFile);
    return isVideoFile(file) ? file : null;
  }

  function pickDroppedFile(dt) {
    // 从拖拽数据中选出第一个视频文件。
    return Array.from(dt?.files || []).find(isVideoFile) || null;
  }

  function useDroppedUrl(dt) {
    // 支持把文本 URL 拖进输入框，行为与手动粘贴一致。
    const text = (dt?.getData("text/uri-list") || dt?.getData("text/plain") || "").trim();
    if (!text) return false;
    clearSelectedFile();
    urlInput.value = text.split(/\s+/)[0];
    scheduleProbe();
    return true;
  }

  urlInput.addEventListener("input", scheduleProbe);
  $("#quality").addEventListener("change", scheduleProbe);
  uploadBtn.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    fileInput.click();
  });
  fileInput.addEventListener("change", () => {
    const file = Array.from(fileInput.files || []).find(isVideoFile);
    if (!file) {
      clearSelectedFile();
      setProbeState("failed", "请选择后端支持的视频文件", true);
      return;
    }
    setSelectedFile(file);
  });

  ["dragenter", "dragover"].forEach((type) => {
    bar.addEventListener(type, (e) => {
      e.preventDefault();
      dragDepth += type === "dragenter" ? 1 : 0;
      bar.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    bar.addEventListener(type, (e) => {
      e.preventDefault();
      dragDepth = type === "drop" ? 0 : Math.max(0, dragDepth - 1);
      if (dragDepth === 0) bar.classList.remove("is-dragover");
    });
  });
  bar.addEventListener("drop", (e) => {
    const file = pickDroppedFile(e.dataTransfer);
    if (file) {
      setSelectedFile(file);
      return;
    }
    if (useDroppedUrl(e.dataTransfer)) return;
    setProbeState("failed", "请拖入视频文件，或粘贴有效的视频链接", true);
  });

  // 「是否需要字幕」：选“仅下载”时禁用字幕相关参数（源/目标语言、模式、烧录、模型、引擎），但保留画质选项
  const paramsBox = form.querySelector(".params");
  function syncSubtitleParams() {
    const need = form.elements.needSubtitle.value === "on";
    if (!need && ttsEnabledInput.checked) {
      ttsEnabledInput.checked = false;
      syncTtsHint();
    }
    paramsBox.querySelectorAll(".param").forEach((p) => {
      if (p.querySelector('[name="needSubtitle"]')) return; // 跳过开关自身
      if (p.id === "qualityParam") return; // 下载画质始终保持可用
      p.classList.toggle("is-disabled", !need);
      p.querySelectorAll("select, input").forEach((c) => {
        c.disabled = !need
          || (c.id === "model" && (!taskWhisperState?.ok || taskWhisperState?.locked))
          || (engineLanguageController?.isLocked() && ["sourceLang", "targetLang"].includes(c.id));
      });
    });
    taskPluginController?.applyControls();
  }
  const ttsEnabledInput = form.elements.ttsEnabled;
  const ttsHint = $("#ttsHint");
  function syncTtsHint() {
    const targetLang = $("#targetLang").value;
    const supported = String(targetLang).toLowerCase().startsWith("zh")
      || String(targetLang).toLowerCase().startsWith("en");
    ttsHint.textContent = ttsHintForTarget(targetLang, ttsEnabledInput.checked);
    ttsHint.classList.toggle("is-error", ttsEnabledInput.checked && !supported);
    return supported;
  }
  ttsEnabledInput.addEventListener("change", syncTtsHint);
  $("#targetLang").addEventListener("change", syncTtsHint);
  syncTtsHint();
  form.querySelectorAll('input[name="needSubtitle"]').forEach((r) =>
    r.addEventListener("change", syncSubtitleParams)
  );
  engineLanguageController.setOnLockChange(() => {
    syncSubtitleParams();
    syncTtsHint();
  });
  syncSubtitleParams();
  taskPluginController = createTaskPluginController({
    root: $("#taskPluginOptions"), form,
    getWhisperState: () => taskWhisperState,
    onChange: () => {
      engineLanguageController.sync();
      syncSubtitleParams();
      syncTtsHint();
      if (probeState.key && probeState.key !== currentProbeKey()) scheduleProbe();
      syncSubmitDisabled();
    },
  });
  taskPluginController?.refresh();
  $("#model").addEventListener("change", () => {
    lastWhisperSelection = $("#model").value;
    syncSubmitDisabled();
  });
  loadEngines();

  $("#engine").addEventListener("change", () => {
    if (engineLanguageController.sync()) toast("本地引擎仅支持英语 → 简体中文", "ph-info");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const videoFile = currentVideoFile();
    const url = urlInput.value.trim();
    if (submitting) return;
    if (taskPluginController) {
      await taskPluginController.refresh({ force: true });
      if (!taskPluginController.isReady()) {
        toast(taskPluginController.error() || "请先修复插件选择", "ph-warning-circle");
        return;
      }
    }
    const selectedEngine = engineLanguageController.selectedEngine();

    if (form.elements.needSubtitle.value === "on" && (!taskPluginController || taskPluginController.requiresWhisperInventory())) {
      const whisperState = await refreshTaskWhisperState({ force: true });
      if (!whisperState?.ok) {
        toast(whisperState?.message || "无法确认 Whisper 模型状态，请稍后重试。", "ph-warning-circle");
        return;
      }
      const selectedModel = $("#model").value;
      if (!whisperState.options.some((item) => item.value === selectedModel)) {
        toast("所选 Whisper 模型已不可用，请在模型设置中修复。", "ph-warning-circle");
        return;
      }
    }

    if (!taskPluginController?.llmBound() && requiresReadyLocalTranslation(selectedEngine, form.elements.needSubtitle.value === "on")) {
      toast("本地模型尚未就绪，请先下载并转换模型", "ph-warning-circle");
      return;
    }

    if (ttsEnabledInput.checked && !syncTtsHint()) {
      $("#targetLang").focus();
      return;
    }

    if (!videoFile && (!url || !isValidUrl(url))) {
      setProbeState("failed", "请输入有效的视频链接（以 http(s):// 开头）", true);
      urlInput.focus();
      return;
    }

    if (!videoFile) {
      const probe = await ensureProbeOk(url);
      if (!probe.ok) {
        urlInput.focus();
        return;
      }
    }

    const collectedPayload = collectTaskPayload(form);
    const payload = {
      ...(taskPluginController ? taskPluginController.applyPayload(collectedPayload) : collectedPayload),
      ...(videoFile ? { file: videoFile } : { url }),
    };

    submitting = true;
    syncSubmitDisabled();
    try {
      await createTask(payload);
      toast("任务已加入队列", "ph-check-circle");
      urlInput.value = "";
      clearSelectedFile();
      resetProbeState();
    } catch (err) {
      toast(err.message || "创建任务失败", "ph-warning-circle");
    } finally {
      submitting = false;
      syncSubmitDisabled();
    }
  });
}
