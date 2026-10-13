/* 独立模型设置页：左侧模型层级导航，右侧按类型编辑配置。 */

import { $, $$ } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";
import { state, setModelKind, subscribe } from "./store.js";

const KINDS = ["llm", "whisper", "tts"];
const LABELS = { llm: "LLM", whisper: "Whisper", tts: "TTS" };
const DEFAULTS = {
  llm: { enabled: true, provider: "deepseek", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", hasApiKey: false, temperature: 0.3, maxTokens: 4096, timeout: 120, concurrency: 2 },
  whisper: { enabled: true, provider: "local", model: "local:small", language: "auto", device: "cpu", threads: 4, wordTimestamps: true, status: "unknown" },
  tts: { enabled: false, provider: "kokoro", baseUrl: "", model: "kokoro-v1", hasApiKey: false, voice: "auto", speed: 1, format: "wav" },
};

const MODEL_NAMES = { llm: "LLM", whisper: "Whisper", tts: "TTS" };
const MODEL_ICONS = { llm: "ph-brain", whisper: "ph-waveform", tts: "ph-speaker-high" };

// Keep a visible local catalog while the optional model settings API is being integrated.
let settings = normalizeModelSettings(DEFAULTS);
let activeKind = "llm";
let loading = false;
let whisperInventory = [];
let refreshEpoch = 0;
let refreshPending = false;
let whisperSaveEpoch = 0;
let whisperSaving = false;
let whisperFormDirty = false;

function normalizedLocalModel(value) {
  return String(value || "").replace(/^local:/, "").trim();
}

function mergeWhisperInventory(config, items = whisperInventory) {
  const ready = items.filter((item) => item.status === "READY");
  const currentName = normalizedLocalModel(config?.model);
  const old = config?.models || [];
  const models = old.map((model) => {
    const inventoryItem = items.find((item) => normalizedLocalModel(item.name) === normalizedLocalModel(model.model));
    return { ...model, status: inventoryItem?.status || "MISSING", isCurrent: false };
  });
  for (const item of ready) {
    const modelName = normalizedLocalModel(item.name);
    const existing = models.find((model) => normalizedLocalModel(model.model) === modelName);
    const readyModel = {
      ...(existing || {}), id: existing?.id || `whisper-${item.name}`, name: item.label || item.name,
      provider: "local", model: `local:${item.name}`, enabled: existing?.enabled ?? config?.enabled !== false, status: "READY",
      isCurrent: false,
    };
    if (existing) Object.assign(existing, readyModel);
    else models.push(readyModel);
  }
  const active = models.find((model) => normalizedLocalModel(model.model) === currentName);
  if (active) active.isCurrent = true;
  const selected = items.find((item) => normalizedLocalModel(item.name) === currentName);
  return { ...config, provider: "local", status: selected?.status || (config?.model ? "MISSING" : "NOT_INSTALLED"), models };
}

function updateWhisperModelOptions() {
  const select = $("#whisperCurrentModel");
  if (!select) return;
  const selected = settings.whisper?.model || "";
  const items = whisperInventory.filter((item) => item.status === "READY");
  const options = items.map((item) => {
    const option = document.createElement("option");
    option.value = `local:${item.name}`;
    option.textContent = item.label || item.name;
    return option;
  });
  const selectedItem = whisperInventory.find((item) => normalizedLocalModel(item.name) === normalizedLocalModel(selected));
  const readyCurrent = items.some((item) => `local:${item.name}` === selected);
  if (selected && !readyCurrent) {
    const option = document.createElement("option");
    const status = String(selectedItem?.status || settings.whisper?.status || "MISSING").toUpperCase();
    const statusLabel = {
      ERROR: "检查失败",
      DOWNLOADING: "下载中",
      MISSING: "缺失",
      NOT_INSTALLED: "未安装",
    }[status] || "缺失";
    const displayName = selectedItem?.label || selectedItem?.name || selected;
    option.value = selected;
    option.textContent = `${displayName} · 当前模型${statusLabel}`;
    option.disabled = true;
    option.selected = true;
    options.unshift(option);
  }
  select.replaceChildren(...options);
  select.value = selected;
  const hint = $("#whisperSettingsHint");
  if (hint) hint.textContent = readyCurrent ? "当前模型已就绪" : selected ? "当前模型缺失或未就绪，请选择一个已就绪模型" : "请选择一个已就绪的本地模型";
}

function setWhisperFeedback(message, isError = false) {
  const feedback = $("#whisperSettingsFeedback");
  if (!feedback) return;
  feedback.hidden = !message;
  feedback.textContent = message;
  feedback.setAttribute("role", isError ? "alert" : "status");
}

function acceptWhisperInventory(items = [], authoritativeWhisper = null) {
  whisperInventory = Array.isArray(items) ? items : [];
  const previousModel = settings.whisper?.model;
  if (authoritativeWhisper && typeof authoritativeWhisper === "object") {
    settings.whisper = normalizeModelSettings({ whisper: authoritativeWhisper }).whisper;
  }
  settings.whisper = mergeWhisperInventory(settings.whisper);
  if (!whisperFormDirty) fillForm("whisper");
  else if (previousModel !== settings.whisper.model) {
    const modelControl = $('[data-model-form="whisper"] [name="model"]');
    if (modelControl) modelControl.value = settings.whisper.model;
    const current = settings.whisper.models?.find((model) => model.isCurrent);
    const meta = $("#modelMetaWhisper");
    if (meta) meta.textContent = `当前模型：${current?.name || settings.whisper.model || "未选择模型"} · ${settings.whisper.status === "READY" ? "已就绪" : settings.whisper.status === "ERROR" ? "检查失败" : settings.whisper.status === "DOWNLOADING" ? "下载中" : "当前模型不可用"} · 本地运行参数由此页管理`;
  }
  applyStatus("whisper");
  updateWhisperModelOptions();
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function normalizeModelSettings(value = {}) {
  return KINDS.reduce((result, kind) => {
    const config = { ...clone(DEFAULTS[kind]), ...(value?.[kind] || {}) };
    const fallback = { id: `${kind}-default`, name: config.model || `${MODEL_NAMES[kind]} 默认模型`, ...config, isCurrent: true };
    const models = Array.isArray(config.models) && config.models.length ? config.models : [fallback];
    const normalizeVoice = (model) => kind === "tts" && model.voice === "af_heart" ? { ...model, voice: "auto" } : model;
    result[kind] = { ...normalizeVoice(config), models: models.map((model, index) => ({ ...normalizeVoice(model), id: model.id || `${kind}-${index + 1}`, isCurrent: model.isCurrent ?? index === 0 })) };
    return result;
  }, {});
}

function setStatus(kind, state, text) {
  const node = $(`#modelStatus${kind[0].toUpperCase()}${kind.slice(1)}`);
  if (node) {
    node.className = `engine-status engine-status--${state}`;
    node.textContent = text;
  }
  const rail = $(`[data-model-status="${kind}"]`);
  if (rail) {
    rail.className = `model-settings__rail-status model-settings__rail-status--${state}`;
    rail.textContent = text;
  }
}

function statusFor(kind, config) {
  if (config?.enabled === false) return ["unconfigured", "未启用"];
  if (kind === "whisper") {
    const status = String(config?.status || "NOT_INSTALLED").toUpperCase();
    if (status === "READY") return ["available", "已就绪"];
    if (status === "ERROR") return ["unavailable", "检查失败"];
    if (status === "DOWNLOADING") return ["checking", "下载中"];
    if (status === "MISSING") return ["unavailable", "当前模型缺失"];
    return ["unknown", "未安装"];
  }
  if (kind === "tts" && config?.provider === "kokoro") return ["available", "本地可用"];
  return [config?.hasApiKey || kind === "whisper" ? "available" : "unconfigured", config?.hasApiKey || kind === "whisper" ? "已配置" : "待配置"];
}

function applyStatus(kind) {
  setStatus(kind, ...statusFor(kind, settings[kind]));
}

function fillForm(kind) {
  const form = $(`[data-model-form="${kind}"]`);
  if (!form) return;
  const config = settings[kind] || DEFAULTS[kind];
  for (const control of $$('input, select', form)) {
    const value = config[control.name];
    if (control.name === "apiKey") control.value = "";
    else if (control.type === "checkbox") control.checked = value !== false;
    else if (value !== undefined && value !== null) {
      if (control.name === "voice" && control.tagName === "SELECT" && !Array.from(control.options || []).some((option) => option.value === String(value))) {
        const option = document.createElement("option");
        option.value = String(value);
        option.textContent = `${value} · 当前保存值`;
        control.append(option);
      }
      control.value = String(value);
    }
  }
  const meta = $(`#modelMeta${kind[0].toUpperCase()}${kind.slice(1)}`);
  if (meta) {
    if (kind === "whisper") {
      const current = config.models?.find((model) => model.isCurrent || normalizedLocalModel(model.model) === normalizedLocalModel(config.model));
      const name = current?.name || config.model || "未选择模型";
      const readiness = String(current?.status || config.status || "").toUpperCase() === "READY" ? "已就绪" : "需检查本地模型库";
      meta.textContent = `当前模型：${name} · ${readiness} · 本地运行参数由此页管理`;
    } else {
      const keyNote = config.hasApiKey ? "API Key 已配置" : "未保存新的密钥";
      meta.textContent = config.updatedAt ? `上次更新 ${new Date(config.updatedAt).toLocaleString()} · ${keyNote}` : keyNote;
    }
  }
  applyStatus(kind);
  renderCatalog(kind);
}

function modelStatus(kind, model) {
  if (model.enabled === false) return ["unconfigured", "未启用"];
  if (kind === "whisper") return [model.status === "READY" ? "available" : "unknown", model.status === "READY" ? "已就绪" : "未安装"];
  if (kind === "tts" && model.provider === "kokoro") return ["available", "本地可用"];
  return model.hasApiKey || model.apiKey ? ["available", "已接入"] : ["unconfigured", "待配置"];
}

function renderCatalog(kind) {
  const grid = $(`[data-model-cards="${kind}"]`);
  if (!grid) return;
  const models = settings[kind]?.models || [];
  grid.replaceChildren(...models.map((model) => {
    const card = document.createElement("article");
    const [status, statusText] = modelStatus(kind, model);
    card.className = `model-card${model.isCurrent ? " is-current" : ""}`;
    card.dataset.modelId = model.id;
    card.tabIndex = 0;
    card.innerHTML = `<div class="model-card__top"><span class="model-card__icon model-card__icon--${kind}"><i class="ph ${MODEL_ICONS[kind]}" aria-hidden="true"></i></span><span class="model-card__status model-card__status--${status}"><i aria-hidden="true"></i>${statusText}</span></div><div class="model-card__body"><h4>${escapeHtml(model.name || model.model || `${MODEL_NAMES[kind]} 模型`)}</h4><p>${escapeHtml(model.model || "未填写模型名称")}</p></div><div class="model-card__foot"><span>${escapeHtml(model.provider || "自定义服务")}</span><button type="button" class="model-card__select" data-model-select="${kind}" data-model-id="${model.id}">${model.isCurrent ? "当前使用" : "设为当前"}</button></div>`;
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      openModelDialog(kind, model);
    });
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openModelDialog(kind, model); } });
    return card;
  }));
  $$('[data-model-select]', grid).forEach((button) => button.addEventListener("click", () => selectCatalogModel(kind, button.dataset.modelId)));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

export async function selectCatalogModel(kind, id) {
  if (kind === "whisper") return selectWhisperModel(id);
  const model = settings[kind]?.models?.find((item) => item.id === id);
  if (!model) return;
  const previous = clone(settings[kind]);
  const models = settings[kind].models.map((item) => ({ ...item, isCurrent: item.id === id }));
  settings[kind] = { ...settings[kind], ...model, model: model.model, provider: model.provider, models };
  try {
    const response = await Api.updateModelSettings({ [kind]: settings[kind] });
    settings = normalizeModelSettings({ ...settings, ...response });
    fillForm(kind);
    toast(`${model.name || model.model} 已设为当前模型`, "ph-check-circle");
  } catch (error) {
    settings[kind] = previous;
    fillForm(kind);
    setStatus(kind, "unavailable", "保存失败");
    toast(error.message || `保存 ${LABELS[kind]} 当前模型失败`, "ph-warning-circle");
  }
}

function cleanWhisperPayload(config) {
  const allowed = new Set(["enabled", "provider", "model", "language", "device", "threads", "wordTimestamps", "baseUrl", "models"]);
  const fields = Object.fromEntries(Object.entries(config).filter(([key]) => allowed.has(key)));
  const modelFields = new Set(["id", "name", "provider", "model", "enabled", "isCurrent", "language", "device", "threads", "wordTimestamps"]);
  const models = (config.models || []).map((model) => Object.fromEntries(Object.entries(model).filter(([key]) => modelFields.has(key))));
  return { ...fields, provider: "local", models };
}

async function saveWhisper(nextConfig, successLabel = "Whisper 设置已保存") {
  if (whisperSaving) return false;
  whisperSaving = true;
  const selector = $("#whisperCurrentModel");
  if (selector) selector.disabled = true;
  const saveEpoch = ++whisperSaveEpoch;
  refreshEpoch += 1;
  const previous = clone(settings.whisper);
  const clean = cleanWhisperPayload(nextConfig);
  try {
    const response = await Api.updateModelSettings({ whisper: clean });
    if (saveEpoch !== whisperSaveEpoch) return false;
    if (!response?.whisper) throw new Error("服务器未返回 Whisper 设置，无法确认保存结果");
    const authoritative = normalizeModelSettings({ ...settings, ...response }).whisper;
    const target = normalizedLocalModel(clean.model);
    if (target && normalizedLocalModel(authoritative.model) !== target) throw new Error("服务器未确认当前 Whisper 模型，请刷新后重试");
    settings.whisper = mergeWhisperInventory(authoritative);
    fillForm("whisper");
    updateWhisperModelOptions();
    document.dispatchEvent(new CustomEvent("model-settings-change", { detail: { settings: { whisper: authoritative }, source: "mutation" } }));
    toast(successLabel, "ph-check-circle");
    setWhisperFeedback(successLabel);
    return true;
  } catch (error) {
    if (saveEpoch !== whisperSaveEpoch) return false;
    settings.whisper = previous;
    fillForm("whisper");
    updateWhisperModelOptions();
    setStatus("whisper", "unavailable", "保存失败");
    setWhisperFeedback(error.message || "保存 Whisper 设置失败", true);
    toast(error.message || "保存 Whisper 设置失败", "ph-warning-circle");
    return false;
  } finally {
    whisperSaving = false;
    if (selector) selector.disabled = false;
  }
}

async function selectWhisperModel(id) {
  const model = settings.whisper?.models?.find((item) => item.id === id);
  if (!model || model.status !== "READY") return;
  if (settings.whisper?.model === model.model) return;
  const models = settings.whisper.models.map((item) => ({ ...item, isCurrent: false }));
  const target = models.find((item) => item.id === id);
  if (target) target.isCurrent = true;
  await saveWhisper({ ...settings.whisper, provider: "local", model: model.model, enabled: true, models }, `${model.name} 已设为当前模型`);
}

function readForm(kind) {
  const form = $(`[data-model-form="${kind}"]`);
  if (!form) return {};
  const data = Object.fromEntries(new FormData(form).entries());
  const payload = { ...data };
  // FormData omits unchecked checkboxes; preserve explicit false values for every
  // boolean field so disabling a feature is persisted instead of silently ignored.
  for (const control of $$('input[type="checkbox"]', form)) {
    payload[control.name] = control.checked;
  }
  for (const key of ["temperature", "maxTokens", "timeout", "concurrency", "threads", "speed"]) {
    if (payload[key] !== undefined && payload[key] !== "") payload[key] = Number(payload[key]);
  }
  if (["llm", "tts"].includes(kind)) {
    const apiKey = payload.apiKey?.trim();
    // An empty password means "keep the existing key" in this form. Omit it
    // from the update payload so the backend cannot interpret it as a clear.
    if (apiKey) payload.apiKey = apiKey;
    else delete payload.apiKey;
  }
  return payload;
}

function selectKind(kind, { syncStore = true } = {}) {
  if (!KINDS.includes(kind)) return;
  activeKind = kind;
  if (syncStore) setModelKind(kind);
  document.dispatchEvent(new CustomEvent("modelkindchange", { detail: { kind } }));
  $$('[data-model-kind]').forEach((button) => {
    const active = button.dataset.modelKind === kind;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $$('[data-model-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.modelPanel !== kind;
    panel.classList.toggle("is-active", panel.dataset.modelPanel === kind);
  });
}

function setLoading(value) {
  loading = value;
  const node = $("#modelSettingsLoading");
  if (node) node.hidden = !value;
  $("#modelSettingsSummary")?.replaceChildren(document.createTextNode(value ? "正在读取" : `${KINDS.filter((kind) => settings[kind]?.enabled !== false).length} 个服务已启用`));
}

function setSummary(text) {
  $("#modelSettingsSummary")?.replaceChildren(document.createTextNode(text));
}

async function refresh() {
  if (loading) { refreshPending = true; return; }
  const epoch = ++refreshEpoch;
  let failed = false;
  setLoading(true);
  KINDS.forEach((kind) => setStatus(kind, "checking", "读取中"));
  try {
    const [modelSettings, inventory] = await Promise.all([Api.getModelSettings(), Api.listLocalModels()]);
    if (epoch !== refreshEpoch) return;
    whisperInventory = Array.isArray(inventory) ? inventory : [];
    settings = normalizeModelSettings(modelSettings);
    settings.whisper = mergeWhisperInventory(settings.whisper);
    document.dispatchEvent(new CustomEvent("model-settings-change", { detail: { settings, source: "refresh" } }));
    KINDS.forEach(fillForm);
    updateWhisperModelOptions();
  } catch (error) {
    if (epoch !== refreshEpoch) return;
    failed = true;
    // A failed refresh must not leave old inventory entries available for activation.
    whisperInventory = [];
    settings.whisper = mergeWhisperInventory(settings.whisper);
    KINDS.forEach(fillForm);
    KINDS.forEach((kind) => setStatus(kind, "unavailable", "接口待接入"));
    updateWhisperModelOptions();
    toast(error.message || "读取模型设置失败", "ph-warning-circle");
  } finally {
    setLoading(false);
    if (failed) setSummary("接口待接入");
    if (refreshPending && state.view === "model-settings") {
      refreshPending = false;
      queueMicrotask(() => void refresh());
    }
  }
}

async function save(kind, event) {
  event.preventDefault();
  const form = $(`[data-model-form="${kind}"]`);
  const button = form?.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  try {
    const values = readForm(kind);
    const current = settings[kind] || DEFAULTS[kind];
    if (kind === "whisper") {
      const selected = values.model;
      if (!selected || !whisperInventory.some((item) => item.status === "READY" && `local:${item.name}` === selected)) {
        throw new Error("请选择一个已就绪的本地 Whisper 模型");
      }
      const models = current.models.map((model) => ({ ...model, isCurrent: false }));
      const targetId = current.models.find((item) => normalizedLocalModel(item.model) === normalizedLocalModel(selected))?.id;
      const target = models.find((model) => model.id === targetId);
      if (target) Object.assign(target, values, { isCurrent: true, model: selected, provider: "local" });
      await saveWhisper({ ...current, ...values, provider: "local", enabled: values.enabled !== false, model: selected, models });
      return;
    }
    const models = current.models || [];
    const activeId = models.find((model) => model.isCurrent)?.id || models[0]?.id;
    const next = {
      ...current,
      ...values,
      models: models.map((model) => ({ ...model, ...(model.id === activeId ? values : {}), isCurrent: model.id === activeId })),
    };
    const response = await Api.updateModelSettings({ [kind]: next });
    settings = normalizeModelSettings({ ...settings, ...response });
    fillForm(kind);
    toast(`${LABELS[kind]} 设置已保存`, "ph-check-circle");
  } catch (error) {
    setStatus(kind, "unavailable", "保存失败");
    toast(error.message || `保存 ${LABELS[kind]} 设置失败`, "ph-warning-circle");
  } finally {
    if (button) button.disabled = false;
  }
}

function openModelDialog(kind, model = null) {
  if (kind === "whisper") return;
  const dialog = $("#modelCreateDialog");
  const form = $("#modelCreateForm");
  if (!dialog || !form) return;
  form.dataset.modelKind = kind;
  form.dataset.modelId = model?.id || "";
  $("#modelCreateDialogTitle").textContent = `新增 ${MODEL_NAMES[kind]} 模型`;
  $("#modelCreateDialogIntro").textContent = `填写 ${MODEL_NAMES[kind]} 连接信息，保存后会出现在当前类型的模型卡片中。`;
  const provider = $("#modelCreateProvider");
  provider.replaceChildren(...(kind === "tts"
      ? [["kokoro", "Kokoro · 本地 CPU"], ["openai", "OpenAI 兼容接口"], ["elevenlabs", "ElevenLabs"]]
      : [["openai", "OpenAI 兼容接口"], ["deepseek", "DeepSeek"], ["custom", "自定义服务"]]).map(([value, label]) => { const option = document.createElement("option"); option.value = value; option.textContent = label; return option; }));
  form.reset();
  form.elements.name.value = model?.name || model?.model || "";
  form.elements.provider.value = model?.provider || provider.options[0]?.value || "";
  form.elements.model.value = model?.model || "";
  form.elements.baseUrl.value = model?.baseUrl || "";
  form.elements.enabled.checked = model?.enabled !== false;
  $("#modelCreateDialogTitle").textContent = model ? `编辑 ${MODEL_NAMES[kind]} 模型` : `新增 ${MODEL_NAMES[kind]} 模型`;
  $("#modelCreateSubmit span").textContent = model ? "保存修改" : "添加模型";
  $("#modelDialogValidate").dataset.modelKind = kind;
  const extra = $("#modelCreateExtraFields");
  extra.replaceChildren(...(kind === "llm"
    ? [field("temperature", "Temperature", model?.temperature ?? 0.3, "number"), field("maxTokens", "上下文长度 tokens", model?.maxTokens ?? 4096, "number"), field("timeout", "请求超时（秒）", model?.timeout ?? 120, "number"), field("concurrency", "并发请求数", model?.concurrency ?? 2, "number")]
    : kind === "whisper"
      ? [field("language", "默认语言", model?.language || "auto"), field("device", "计算设备", model?.device || "cpu"), field("threads", "线程数", model?.threads ?? 4, "number"), checkField("wordTimestamps", "生成词级时间戳", model?.wordTimestamps !== false)]
      : [field("voice", "默认音色", model?.voice || "auto"), field("speed", "语速", model?.speed ?? 1, "number"), field("format", "输出格式", model?.format || "wav")]).filter(Boolean));
  $("#modelCreateFeedback").hidden = true;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  form.elements.name.focus();
}

async function validateDialog() {
  const form = $("#modelCreateForm");
  const button = $("#modelDialogValidate");
  const feedback = $("#modelCreateFeedback");
  const kind = form?.dataset.modelKind;
  if (!form || !kind || !form.reportValidity()) return;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.enabled = form.elements.enabled.checked;
  if (form.elements.wordTimestamps) payload.wordTimestamps = form.elements.wordTimestamps.checked;
  for (const key of ["temperature", "maxTokens", "timeout", "concurrency", "threads", "speed"]) {
    if (payload[key] !== undefined && payload[key] !== "") payload[key] = Number(payload[key]);
  }
  if (!payload.apiKey?.trim()) delete payload.apiKey;
  button.disabled = true;
  try {
    const result = await Api.validateModelSettings(kind, payload);
    if (!result?.ok) throw new Error(result?.message || "模型检查未通过");
    feedback.textContent = result.message || "连接成功";
    feedback.className = "model-dialog__feedback model-dialog__feedback--success";
    feedback.hidden = false;
  } catch (error) {
    feedback.textContent = error.message || "模型连接检查失败";
    feedback.className = "model-dialog__feedback";
    feedback.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function field(name, label, value, type = "text") {
  const wrapper = document.createElement("label");
  wrapper.className = "model-field";
  wrapper.innerHTML = `<span>${escapeHtml(label)}</span><input name="${escapeHtml(name)}" type="${escapeHtml(type)}" value="${escapeHtml(value)}" />`;
  return wrapper;
}

function checkField(name, label, checked) {
  const wrapper = document.createElement("label");
  wrapper.className = "model-dialog__switch model-dialog__switch--extra";
  wrapper.innerHTML = `<input name="${escapeHtml(name)}" type="checkbox" ${checked ? "checked" : ""} /><span><strong>${escapeHtml(label)}</strong><small>保存词级时间戳设置</small></span>`;
  return wrapper;
}

async function createModel(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const kind = form.dataset.modelKind;
  if (!kind) return;
  if (!form.reportValidity()) return;
  const submit = $("#modelCreateSubmit");
  const feedback = $("#modelCreateFeedback");
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.enabled = form.elements.enabled.checked;
  if (form.elements.wordTimestamps) payload.wordTimestamps = form.elements.wordTimestamps.checked;
  for (const key of ["temperature", "maxTokens", "timeout", "concurrency", "threads", "speed"]) {
    if (payload[key] !== undefined && payload[key] !== "") payload[key] = Number(payload[key]);
  }
  if (!payload.apiKey?.trim()) delete payload.apiKey;
  submit.disabled = true;
  feedback.hidden = true;
  try {
    const editingId = form.dataset.modelId;
    if (editingId) {
      const current = settings[kind] || DEFAULTS[kind];
      const updatedModel = { ...(current.models || []).find((item) => item.id === editingId), ...payload, id: editingId };
      const models = (current.models || []).map((item) => item.id === editingId ? updatedModel : item);
      const response = await Api.updateModelSettings({ [kind]: { models } });
      settings = normalizeModelSettings({ ...settings, ...response });
      fillForm(kind);
      toast(`${LABELS[kind]} 模型已更新`, "ph-check-circle");
    } else {
      const created = await Api.createModelSettings(kind, payload);
      const current = settings[kind] || DEFAULTS[kind];
      settings[kind] = { ...current, ...(created.config || {}) };
      settings[kind].models = settings[kind].models.map((model, index) => ({ ...model, isCurrent: created.model?.id ? model.id === created.model.id : index === settings[kind].models.length - 1 }));
      fillForm(kind);
      setStatus(kind, "available", "已接入");
      toast(`${LABELS[kind]} 模型已添加`, "ph-check-circle");
    }
    if (typeof $("#modelCreateDialog").close === "function") $("#modelCreateDialog").close();
    else $("#modelCreateDialog").removeAttribute("open");
  } catch (error) {
    feedback.textContent = error.message || "新增模型失败";
    feedback.hidden = false;
  } finally { submit.disabled = false; }
}

async function validate(kind) {
  const button = $(`[data-model-validate="${kind}"]`);
  if (button) button.disabled = true;
  setStatus(kind, "checking", "检查中");
  try {
    const result = await Api.validateModelSettings(kind, { ...settings[kind], ...readForm(kind) });
    if (!result?.ok) throw new Error(result?.message || "模型检查未通过");
    settings[kind] = { ...settings[kind], status: "ready" };
    setStatus(kind, "available", "检查通过");
    toast(result.message || `${LABELS[kind]} 检查通过`, "ph-check-circle");
  } catch (error) {
    setStatus(kind, "unavailable", "检查失败");
    toast(error.message || `${LABELS[kind]} 检查失败`, "ph-warning-circle");
  } finally {
    if (button) button.disabled = false;
  }
}

export function initModelSettings() {
  const root = $("#modelSettings");
  if (!root) return;
  $$('[data-model-kind]', root).forEach((button) => button.addEventListener("click", () => selectKind(button.dataset.modelKind)));
  $$('[data-model-form]', root).forEach((form) => form.addEventListener("submit", (event) => save(form.dataset.modelForm, event)));
  $$('[data-model-validate]', root).forEach((button) => button.addEventListener("click", () => validate(button.dataset.modelValidate)));
  $$('[data-model-add]', root).forEach((button) => button.addEventListener("click", () => openModelDialog(button.dataset.modelAdd)));
  $("#modelCreateForm")?.addEventListener("submit", createModel);
  $("#modelDialogValidate")?.addEventListener("click", validateDialog);
  $$('[data-model-dialog-close]').forEach((button) => button.addEventListener("click", () => {
    const dialog = $("#modelCreateDialog");
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
  }));
  $("#modelSettingsCollapse")?.addEventListener("click", () => {
    const collapsed = root.classList.toggle("is-rail-collapsed");
    const button = $("#modelSettingsCollapse");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "展开模型导航" : "收起模型导航");
    button.title = collapsed ? "展开模型导航" : "收起模型导航";
  });
  document.addEventListener("whisper-models-change", (event) => acceptWhisperInventory(event.detail?.items, event.detail?.whisper));
  document.addEventListener("whisper-model-select", async (event) => {
    const wanted = normalizedLocalModel(event.detail?.name);
    const model = settings.whisper?.models?.find((item) => normalizedLocalModel(item.model) === wanted && item.status === "READY");
    if (model) await selectWhisperModel(model.id);
  });
  $("#whisperCurrentModel")?.addEventListener("change", async (event) => {
    const model = settings.whisper?.models?.find((item) => item.model === event.target.value);
    if (model) await selectWhisperModel(model.id);
  });
  subscribe(({ type }) => {
    if (["model-kind", "legacy-model-navigation"].includes(type) && state.modelKind !== activeKind) selectKind(state.modelKind, { syncStore: false });
  });
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view !== "model-settings") return;
    const locationKind = new URLSearchParams(window.location?.search || "").get("kind");
    const desiredKind = locationKind === "whisper" ? "whisper" : state.modelKind;
    if (desiredKind !== activeKind) selectKind(desiredKind, { syncStore: false });
    if (activeKind !== "whisper") void refresh();
  });
  selectKind(state.modelKind || activeKind);
  KINDS.forEach(fillForm);
  const whisperForm = $('[data-model-form="whisper"]');
  whisperForm?.addEventListener("input", () => { whisperFormDirty = true; });
  whisperForm?.addEventListener("change", () => { whisperFormDirty = true; });
  whisperForm?.addEventListener("submit", () => { whisperFormDirty = false; });
  if (state.view === "model-settings" && activeKind !== "whisper") void refresh();
}
