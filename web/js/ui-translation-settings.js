/* 高级设置：多服务商引擎配置、脱敏状态与连接检测。 */

import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";
import { state } from "./store.js";

const STATUS = {
  AVAILABLE: ["available", "检测成功"],
  CHECKING: ["checking", "检测中"],
  UNKNOWN: ["unknown", "待检测"],
  UNAVAILABLE: ["unavailable", "检测失败"],
  UNCONFIGURED: ["unconfigured", "未配置"],
};

const LOCAL_ENGINE_ID = "local-opus-en-zh";
const LOCAL_MODEL_STATUS = {
  NOT_INSTALLED: ["idle", "尚未下载"],
  DOWNLOADING: ["busy", "下载中"],
  CONVERTING: ["busy", "转换中"],
  CHECKING: ["busy", "离线检测中"],
  READY: ["ready", "已就绪"],
  FAILED: ["error", "处理失败"],
};
const LOCAL_ENGINE_DEFAULT = {
  id: LOCAL_ENGINE_ID,
  name: "本地 CPU 英译中",
  apiType: "local_ct2",
  model: "Helsinki-NLP/opus-mt-en-zh",
  baseUrl: "",
  enabled: true,
  hasApiKey: false,
  availability: "UNAVAILABLE",
  modelStatus: "NOT_INSTALLED",
  installedBytes: 0,
  supportedSourceLanguages: ["en"],
  supportedTargetLanguages: ["zh-CN", "zh"],
};
const INSTALLING_STATES = new Set(["DOWNLOADING", "CONVERTING", "CHECKING"]);
const POLL_INTERVAL_MS = 1500;

const DEFAULT_DEEPSEEK = {
  id: "deepseek",
  name: "DeepSeek",
  apiType: "openai_compatible",
  baseUrl: "https://api.deepseek.com",
  model: "deepseek-chat",
  enabled: true,
  hasApiKey: false,
  availability: "UNCONFIGURED",
};

let engines = [];
let modelPollTimer = null;

export function statusView(value) {
  return STATUS[String(value || "").toUpperCase()] || STATUS.UNKNOWN;
}

function typeLabel(value) {
  if (value === "local_ct2") return "本地模型 · CPU";
  return value === "anthropic_compatible" ? "Anthropic Compatible" : "OpenAI Compatible";
}

export function localModelStatusView(value) {
  return LOCAL_MODEL_STATUS[String(value || "NOT_INSTALLED").toUpperCase()] || LOCAL_MODEL_STATUS.NOT_INSTALLED;
}

export function isLocalEngineReady(engine) {
  return engine?.id === LOCAL_ENGINE_ID && String(engine.modelStatus || "").toUpperCase() === "READY";
}

export function formatInstalledModelSize(value) {
  const bytes = Number(value) || 0;
  if (!bytes) return "未安装";
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

export function localEngineStateChanged(previous, next) {
  return Boolean(previous && next && (
    previous.modelStatus !== next.modelStatus || previous.availability !== next.availability
  ));
}

export function dispatchLocalEngineStateChange(previous, next, dispatch) {
  if (!localEngineStateChanged(previous, next)) return false;
  dispatch();
  return true;
}

function notifyLocalEngineStateChange(previous, next) {
  dispatchLocalEngineStateChange(previous, next, () => {
    document.dispatchEvent(new CustomEvent("translation-engines-change"));
  });
}

function isValidationSuccess(result) {
  return result?.available === true || String(result?.availability || "").toUpperCase() === "AVAILABLE";
}

export function localEngineCardTemplate(engine) {
  const [statusClass, statusText] = localModelStatusView(engine.modelStatus);
  const busy = INSTALLING_STATES.has(String(engine.modelStatus || "").toUpperCase());
  const size = formatInstalledModelSize(engine.installedBytes);
  const detail = engine.modelError || (isLocalEngineReady(engine) ? "可在任务页选择此引擎" : "首次使用需要下载模型文件");
  return `<article class="engine-card local-engine-card" data-engine-id="${LOCAL_ENGINE_ID}">
    <div class="engine-card__top"><div class="engine-card__identity"><span class="engine-card__icon"><i class="ph ph-cpu"></i></span><div><h3 class="engine-card__name">本地 CPU 英译中</h3><div class="engine-card__type">Helsinki-NLP/opus-mt-en-zh</div></div></div><span class="engine-status engine-status--${statusClass}">${statusText}</span></div>
    <div class="local-engine-card__details"><span>英语 → 简体中文</span><span>CPU 推理 · ${size}</span></div>
    <p class="local-engine-card__message"${engine.modelError ? ' role="alert"' : ''}>${escapeHtml(detail)}</p>
    <div class="engine-card__foot"><label class="engine-card__meta"><input data-field="enabled" type="checkbox" ${engine.enabled !== false ? "checked" : ""} /> 在任务页启用</label><div class="engine-card__actions">
      <button class="btn btn--ghost btn--sm" data-action="download" type="button"${busy || isLocalEngineReady(engine) ? " disabled" : ""}><i class="ph ph-download-simple"></i><span>${busy ? statusText : isLocalEngineReady(engine) ? "已下载" : "下载并转换"}</span></button>
      <button class="btn btn--ghost btn--sm" data-action="validate" type="button"${busy ? ' disabled aria-busy="true"' : ""}><i class="ph ph-plugs-connected"></i><span>${String(engine.modelStatus || "").toUpperCase() === "CHECKING" ? "检测中" : "离线检测"}</span></button>
      <button class="btn btn--primary btn--sm" data-action="save" type="button"><i class="ph ph-floppy-disk"></i><span>保存</span></button>
    </div></div>
  </article>`;
}

function updateEngine(id, patch) {
  const previous = engines.find((engine) => engine.id === id);
  engines = engines.map((engine) => engine.id === id ? { ...engine, ...patch } : engine);
  render();
  if (id === LOCAL_ENGINE_ID) notifyLocalEngineStateChange(previous, engines.find((engine) => engine.id === id));
}

async function validateEngine(id) {
  updateEngine(id, { availability: "CHECKING", lastError: null });
  try {
    const result = await Api.validateTranslationEngine(id);
    const availability = String(
      result?.availability || (result?.available ? "AVAILABLE" : "UNAVAILABLE"),
    ).toUpperCase();
    updateEngine(id, {
      availability,
      lastCheckedAt: result?.checkedAt || Date.now(),
      lastError: result?.message || null,
    });
    return { ...result, availability };
  } catch (err) {
    updateEngine(id, {
      availability: "UNAVAILABLE",
      lastCheckedAt: Date.now(),
      lastError: err.message || "检测请求失败",
    });
    throw new Error(`检测失败：${err.message || "请求未完成"}`);
  }
}

function notifyValidation(result) {
  const success = isValidationSuccess(result);
  const detail = result?.message ? `：${result.message}` : "";
  toast(`${success ? "检测成功" : "检测失败"}${detail}`, success ? "ph-check-circle" : "ph-warning-circle");
}

function cardTemplate(engine) {
  if (engine.id === LOCAL_ENGINE_ID || engine.apiType === "local_ct2") {
    return localEngineCardTemplate(engine);
  }
  const status = statusView(engine.availability);
  const id = engine.id || "new";
  const isNew = !engine.id;
  return `<article class="engine-card" data-engine-id="${escapeHtml(id)}">
    <div class="engine-card__top">
      <div class="engine-card__identity">
        <span class="engine-card__icon"><i class="ph ${engine.apiType === "anthropic_compatible" ? "ph-aperture" : "ph-brackets-curly"}"></i></span>
        <div><h3 class="engine-card__name">${escapeHtml(engine.name || "新翻译引擎")}</h3><div class="engine-card__type">${typeLabel(engine.apiType)}</div></div>
      </div>
        <span class="engine-status engine-status--${status[0]}">${engine.active ? "当前运行配置 · " : ""}${status[1]}</span>
    </div>
    <div class="engine-card__form">
      <div class="engine-field"><label>显示名称</label><input data-field="name" value="${escapeHtml(engine.name || "")}" placeholder="例如：DeepSeek 主力" /></div>
      <div class="engine-card__grid">
        <div class="engine-field"><label>API 接入类型</label><select data-field="apiType"><option value="openai_compatible" ${engine.apiType !== "anthropic_compatible" ? "selected" : ""}>OpenAI Compatible</option><option value="anthropic_compatible" ${engine.apiType === "anthropic_compatible" ? "selected" : ""}>Anthropic Compatible</option></select></div>
        <div class="engine-field"><label>模型</label><input data-field="model" value="${escapeHtml(engine.model || "")}" placeholder="例如：gpt-4.1-mini" /></div>
      </div>
      <div class="engine-field"><label>Base URL</label><input data-field="baseUrl" value="${escapeHtml(engine.baseUrl || "")}" placeholder="https://api.openai.com/v1" /></div>
      <div class="engine-field"><label>API Key ${engine.hasApiKey ? "<span style=\"color:var(--ok-text)\">· 已配置，留空保持不变</span>" : ""}</label><input data-field="apiKey" type="password" autocomplete="new-password" placeholder="${engine.hasApiKey ? "已配置 · 不修改" : "粘贴 API Key"}" /></div>
    </div>
    <div class="engine-card__foot">
      <label class="engine-card__meta"><input data-field="enabled" type="checkbox" ${engine.enabled !== false ? "checked" : ""} /> 在任务页启用</label>
      <div class="engine-card__actions">
        ${!isNew ? `<button class="btn btn--ghost btn--sm" data-action="validate" type="button"${status[0] === "checking" ? " disabled aria-busy=\"true\"" : ""}><i class="ph ph-plugs-connected"></i><span>${status[0] === "checking" ? "检测中" : "检测"}</span></button>` : ""}
        <button class="btn btn--primary btn--sm" data-action="save" type="button"><i class="ph ph-floppy-disk"></i><span>保存</span></button>
        ${!isNew ? `<button class="iconbtn" data-action="delete" type="button" title="删除配置"><i class="ph ph-trash"></i></button>` : ""}
      </div>
    </div>
  </article>`;
}

function values(card) {
  const get = (name) => card.querySelector(`[data-field="${name}"]`);
  return {
    name: get("name").value.trim(), apiType: get("apiType").value,
    baseUrl: get("baseUrl").value.trim(), model: get("model").value.trim(),
    apiKey: get("apiKey").value.trim(), enabled: get("enabled").checked,
  };
}

async function refresh(autoCheck = false) {
  try {
    const previousLocal = engines.find((engine) => engine.id === LOCAL_ENGINE_ID);
    engines = await Api.listTranslationEngines();
    if (!engines.length) engines = [{ ...DEFAULT_DEEPSEEK }];
    if (!engines.some((engine) => engine.id === LOCAL_ENGINE_ID)) engines.push({ ...LOCAL_ENGINE_DEFAULT });
    const nextLocal = engines.find((engine) => engine.id === LOCAL_ENGINE_ID);
    // DeepSeek 是内置引擎；旧数据库或服务暂时不可用时也保持稳定的界面入口。
    render();
    syncModelPolling();
    notifyLocalEngineStateChange(previousLocal, nextLocal);
    if (autoCheck) {
      // 每次应用启动都重新检测已配置引擎，避免把上一次启动的结果当成当前状态。
      const configured = engines.filter((e) => e.id && e.hasApiKey);
      await Promise.allSettled(configured.map((engine) => validateEngine(engine.id)));
    }
  } catch (err) {
    const previousLocal = engines.find((engine) => engine.id === LOCAL_ENGINE_ID);
    engines = [{ ...DEFAULT_DEEPSEEK }, { ...LOCAL_ENGINE_DEFAULT }];
    const nextLocal = engines.find((engine) => engine.id === LOCAL_ENGINE_ID);
    render();
    notifyLocalEngineStateChange(previousLocal, nextLocal);
    toast(err.message || "无法读取翻译引擎配置", "ph-warning-circle");
  }
}

function stopModelPolling() {
  if (modelPollTimer) clearTimeout(modelPollTimer);
  modelPollTimer = null;
}

function syncModelPolling() {
  stopModelPolling();
  const local = engines.find((engine) => engine.id === LOCAL_ENGINE_ID);
  const settingsVisible = state.view === "other-settings";
  if (!settingsVisible || !INSTALLING_STATES.has(String(local?.modelStatus || "").toUpperCase())) return;
  modelPollTimer = setTimeout(async () => {
    modelPollTimer = null;
    await refresh(false);
  }, POLL_INTERVAL_MS);
}

async function startLocalAction(id, action) {
  updateEngine(id, { modelStatus: action === "download" ? "DOWNLOADING" : "CHECKING", modelError: null });
  try {
    if (action === "download") await Api.downloadTranslationEngine(id);
    else {
      const result = await Api.validateTranslationEngine(id);
      if (result?.modelStatus) updateEngine(id, result);
      else await refresh(false);
      const current = engines.find((engine) => engine.id === id);
      const success = current?.modelStatus === "READY" || result?.available === true || String(result?.availability || "").toUpperCase() === "AVAILABLE";
      const detail = result?.message ? `：${result.message}` : success ? "：本地模型可用" : "：请先下载并转换模型";
      toast(`${success ? "离线检测成功" : "离线检测失败"}${detail}`, success ? "ph-check-circle" : "ph-warning-circle");
    }
    if (action === "download") await refresh(false);
    syncModelPolling();
  } catch (err) {
    updateEngine(id, { modelStatus: "FAILED", modelError: err.message || "请求未完成" });
    toast(err.message || "本地模型操作失败", "ph-warning-circle");
  }
}

function render() {
  const list = $("#engineList");
  if (!list) return;
  list.innerHTML = engines.map(cardTemplate).join("");
}

async function onAction(event) {
  const action = event.target.closest("[data-action]")?.dataset.action;
  const card = event.target.closest(".engine-card");
  if (!action || !card) return;
  const id = card.dataset.engineId;
  try {
    if (action === "save") {
      if (id === LOCAL_ENGINE_ID) {
        await Api.updateTranslationEngine(id, {
          name: LOCAL_ENGINE_DEFAULT.name,
          apiType: "local_ct2",
          model: LOCAL_ENGINE_DEFAULT.model,
          baseUrl: "",
          apiKey: null,
          enabled: card.querySelector('[data-field="enabled"]')?.checked !== false,
        });
        await refresh(false);
        document.dispatchEvent(new CustomEvent("translation-engines-change"));
        toast("本地翻译引擎设置已保存", "ph-check-circle");
        return;
      }
      const payload = values(card);
      if (!payload.name || !payload.baseUrl || !payload.model) throw new Error("请填写名称、Base URL 和模型");
      const saved = id === "new" ? await Api.createTranslationEngine(payload) : await Api.updateTranslationEngine(id, payload);
      // 重新读取列表，拿到后端计算的当前运行配置标记。
      await refresh(false);
      if (saved.hasApiKey) {
        const result = await validateEngine(saved.id);
        notifyValidation(result);
      } else {
        toast("翻译引擎已保存，未配置 API Key", "ph-info");
      }
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
      return;
    }
    if (action === "validate") {
      if (id === LOCAL_ENGINE_ID) {
        await startLocalAction(id, "validate");
        document.dispatchEvent(new CustomEvent("translation-engines-change"));
        return;
      }
      const result = await validateEngine(id);
      notifyValidation(result);
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
      return;
    }
    if (action === "download" && id === LOCAL_ENGINE_ID) {
      await startLocalAction(id, "download");
      return;
    }
    if (action === "delete" && window.confirm("删除这个翻译引擎配置？")) {
      await Api.deleteTranslationEngine(id);
      engines = engines.filter((e) => e.id !== id);
      render();
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
    }
  } catch (err) {
    toast(err.message || "操作失败", "ph-warning-circle");
  }
}

export function initTranslationSettings() {
  const add = $("#engineAdd");
  const list = $("#engineList");
  if (!add || !list) return;
  add.addEventListener("click", () => {
    engines.push({ apiType: "openai_compatible", availability: "UNCONFIGURED", enabled: true });
    render();
    list.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  list.addEventListener("click", onAction);
  let startupCheckPromise = refresh(true);
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view !== "other-settings" || event.detail?.settingsTab !== "engines" || state.view !== "other-settings") {
      stopModelPolling();
      return;
    }
    // 如果用户在启动检测完成前打开设置页，等待它结束，避免旧列表覆盖检测中的状态。
    startupCheckPromise.then(() => refresh(false));
  });
}
