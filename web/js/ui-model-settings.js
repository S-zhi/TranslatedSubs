/* 独立模型设置页：左侧模型层级导航，右侧按类型编辑配置。 */

import { $, $$ } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";
import { state } from "./store.js";

const KINDS = ["llm", "whisper", "tts"];
const LABELS = { llm: "LLM", whisper: "Whisper", tts: "TTS" };
const DEFAULTS = {
  llm: { enabled: true, provider: "deepseek", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", hasApiKey: false, temperature: 0.3, maxTokens: 4096, timeout: 120, concurrency: 2 },
  whisper: { enabled: true, provider: "local", model: "local:small", language: "auto", device: "cpu", threads: 4, wordTimestamps: true, status: "unknown" },
  tts: { enabled: false, provider: "kokoro", baseUrl: "", model: "kokoro-v1", hasApiKey: false, voice: "af_heart", speed: 1, format: "wav" },
};

const MODEL_NAMES = { llm: "LLM", whisper: "Whisper", tts: "TTS" };
const MODEL_ICONS = { llm: "ph-brain", whisper: "ph-waveform", tts: "ph-speaker-high" };

// Keep a visible local catalog while the optional model settings API is being integrated.
let settings = normalizeModelSettings(DEFAULTS);
let activeKind = "llm";
let loading = false;

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function normalizeModelSettings(value = {}) {
  return KINDS.reduce((result, kind) => {
    const config = { ...clone(DEFAULTS[kind]), ...(value?.[kind] || {}) };
    const fallback = { id: `${kind}-default`, name: config.model || `${MODEL_NAMES[kind]} 默认模型`, ...config, isCurrent: true };
    const models = Array.isArray(config.models) && config.models.length ? config.models : [fallback];
    result[kind] = { ...config, models: models.map((model, index) => ({ ...model, id: model.id || `${kind}-${index + 1}`, isCurrent: model.isCurrent ?? index === 0 })) };
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
  if (kind === "whisper" && config?.provider === "local") {
    return [String(config.status || "unknown").toLowerCase() === "ready" ? "available" : "unknown", String(config.status || "待检查").toLowerCase() === "ready" ? "已就绪" : "待检查"];
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
    else if (value !== undefined && value !== null) control.value = String(value);
  }
  const meta = $(`#modelMeta${kind[0].toUpperCase()}${kind.slice(1)}`);
  if (meta) {
    const keyNote = ["llm", "tts"].includes(kind) && config.hasApiKey ? "API Key 已配置" : "未保存新的密钥";
    meta.textContent = config.updatedAt ? `上次更新 ${new Date(config.updatedAt).toLocaleString()} · ${keyNote}` : keyNote;
  }
  applyStatus(kind);
  renderCatalog(kind);
}

function modelStatus(kind, model) {
  if (model.enabled === false) return ["unconfigured", "未启用"];
  if (kind === "whisper" && model.provider === "local") return ["available", "本地可用"];
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

function selectCatalogModel(kind, id) {
  const model = settings[kind]?.models?.find((item) => item.id === id);
  if (!model) return;
  settings[kind].models = settings[kind].models.map((item) => ({ ...item, isCurrent: item.id === id }));
  settings[kind] = { ...settings[kind], ...model, model: model.model, provider: model.provider };
  fillForm(kind);
  toast(`${model.name || model.model} 已设为当前模型`, "ph-check-circle");
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

function selectKind(kind) {
  if (!KINDS.includes(kind)) return;
  activeKind = kind;
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
  if (loading) return;
  let failed = false;
  setLoading(true);
  KINDS.forEach((kind) => setStatus(kind, "checking", "读取中"));
  try {
    settings = normalizeModelSettings(await Api.getModelSettings());
    KINDS.forEach(fillForm);
  } catch (error) {
    failed = true;
    KINDS.forEach(fillForm);
    KINDS.forEach((kind) => setStatus(kind, "unavailable", "接口待接入"));
    toast(error.message || "读取模型设置失败", "ph-warning-circle");
  } finally {
    setLoading(false);
    if (failed) setSummary("接口待接入");
  }
}

async function save(kind, event) {
  event.preventDefault();
  const form = $(`[data-model-form="${kind}"]`);
  const button = form?.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  try {
    const next = { ...settings[kind], ...readForm(kind), models: settings[kind]?.models || [] };
    const response = await Api.updateModelSettings({ ...settings, [kind]: next });
    settings = normalizeModelSettings(response);
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
  const dialog = $("#modelCreateDialog");
  const form = $("#modelCreateForm");
  if (!dialog || !form) return;
  form.dataset.modelKind = kind;
  form.dataset.modelId = model?.id || "";
  $("#modelCreateDialogTitle").textContent = `新增 ${MODEL_NAMES[kind]} 模型`;
  $("#modelCreateDialogIntro").textContent = `填写 ${MODEL_NAMES[kind]} 连接信息，保存后会出现在当前类型的模型卡片中。`;
  const provider = $("#modelCreateProvider");
  provider.replaceChildren(...(kind === "whisper"
    ? [["local", "本地模型"], ["replicate", "Replicate 云端"], ["custom", "自定义服务"]]
    : kind === "tts"
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
      : [field("voice", "默认音色", model?.voice || "af_heart"), field("speed", "语速", model?.speed ?? 1, "number"), field("format", "输出格式", model?.format || "wav")]).filter(Boolean));
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
      const response = await Api.updateModelSettings({ ...settings, [kind]: { ...current, ...payload, models: (current.models || []).map((item) => item.id === editingId ? updatedModel : item) } });
      settings = normalizeModelSettings(response);
      fillForm(kind);
      toast(`${LABELS[kind]} 模型已更新`, "ph-check-circle");
    } else {
      const created = await Api.createModelSettings(kind, payload);
      const current = settings[kind] || DEFAULTS[kind];
      settings[kind] = { ...current, ...(created.config || {}), models: [...(current.models || []), created.model || created] };
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
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "model-settings") void refresh();
  });
  selectKind(activeKind);
  KINDS.forEach(fillForm);
  if (state.view === "model-settings") void refresh();
}

