import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { analyzeModelFiles, filePath, suggestModelId } from "./local-model-import.js";
import { toast } from "./toast.js";

let pollTimer = null;
let selectedFiles = [];
let modelNameTouched = false;
let lastSuggestedName = "";
let isImporting = false;
let refreshInFlight = null;
let inventoryNames = [];
let currentWhisperModel = "";
let refreshGeneration = 0;
let whisperPageActive = false;
let inventoryItems = [];
let currentWhisperSettings = null;
let forceRefreshQueued = false;
let maxActiveRefreshes = 0;

function actionFeedback(message, error = false) {
  const node = $("#localModelActionFeedback");
  if (node) {
    node.hidden = !message;
    node.textContent = message;
    node.setAttribute("role", error ? "alert" : "status");
    return;
  }
  if (message) toast(message, error ? "ph-warning-circle" : "ph-check-circle");
}

function syncPollTimer(active) {
  if (active && !pollTimer) pollTimer = setInterval(() => {
    if (document.visibilityState === "visible" && whisperPageActive) void refresh();
  }, 2500);
  else if (!active && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function emitInventory(items) {
  inventoryItems = items;
  inventoryNames = items.map((item) => item.name).filter(Boolean);
  document.dispatchEvent(new CustomEvent("whisper-models-change", { detail: { items, whisper: currentWhisperSettings } }));
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function render(items) {
  const root = $("#localModelsList");
  if (!root) return;
  const official = items.filter((item) => item.source === "official");
  const importedReady = items.filter((item) => item.source === "imported" && item.status === "READY");
  const summary = $("#localModelSummary");
  if (summary) summary.textContent = `${official.length} 款官方模型 · ${importedReady.length} 个本地模型就绪`;
  const card = (item) => {
    const name = escapeHtml(item.name);
    const label = escapeHtml(item.label || item.name);
    const busy = item.status === "DOWNLOADING";
    const installed = item.status === "READY";
    const error = item.status === "ERROR";
    const statusLabel = installed ? "已就绪" : busy ? "下载中" : error ? "下载失败" : "未安装";
    const statusClass = installed ? "ready" : busy ? "busy" : error ? "error" : "idle";
    const size = installed && item.installedBytes ? formatBytes(item.installedBytes) : item.size || "大小未知";
    const current = currentWhisperModel.replace(/^local:/, "") === String(item.name);
    return `<article class="local-model-card${busy ? " is-downloading" : ""}${current ? " is-current" : ""}" data-model-name="${name}" aria-label="${label}">
      <div class="local-model-card__top">
        <div class="local-model-card__identity"><span class="local-model-card__icon"><i class="ph ph-cpu" aria-hidden="true"></i></span><div><h3>${label}</h3><span class="local-model-card__name">${name}</span></div></div>
        <span class="local-model-card__status local-model-card__status--${statusClass}">${statusLabel}</span>
      </div>
      <div class="local-model-card__meta"><span>${item.source === "imported" ? "本地导入 · Hugging Face" : "官方 · CTranslate2"}</span><strong>${escapeHtml(size)}</strong></div>
      ${busy ? `<progress class="local-model-card__progress" aria-label="${label} 下载中"></progress>` : ""}
      ${error ? `<p class="local-model-card__error" title="${escapeHtml(item.error || "")}">${escapeHtml(item.error || "下载失败，请重试")}</p>` : ""}
      <div class="local-model-card__foot">
        <span class="local-model-card__phase">${current ? "当前使用" : busy ? item.phase === "checking" ? "正在验证模型" : "正在获取模型文件" : installed ? "可在任务中使用" : "下载后可在任务中使用"}</span>
        <div class="local-model-actions">
          ${installed ? `<button class="btn btn--${current ? "ghost" : "primary"} btn--sm" type="button" data-model-current="${name}"${current ? " disabled aria-current=\"true\"" : ""}>${current ? "当前模型" : "设为当前"}</button>` : ""}
          ${installed && item.format === "huggingface" ? `<button class="btn btn--ghost btn--sm" type="button" data-model-check="${name}" title="检查模型" aria-label="检查 ${label}"><i class="ph ph-shield-check" aria-hidden="true"></i></button>` : ""}
          ${installed ? `<button class="btn btn--ghost btn--sm local-model-card__delete" type="button" data-model-delete="${name}" title="删除模型" aria-label="删除 ${label}"><i class="ph ph-trash" aria-hidden="true"></i><span>删除</span></button>` : item.source === "official" ? `<button class="btn btn--primary btn--sm" type="button" data-model-download="${name}"${busy ? " disabled" : ""}><i class="ph ph-download-simple" aria-hidden="true"></i><span>${busy ? "下载中" : error ? "重试" : "下载"}</span></button>` : ""}
        </div>
      </div>
    </article>`;
  };
  root.innerHTML = official.map(card).join("");
  const importedRoot = $("#importedModelsList");
  if (importedRoot) {
    const imported = items.filter((item) => item.source === "imported");
    importedRoot.innerHTML = imported.length ? imported.map(card).join("") : `<div class="local-model-empty"><span class="local-model-empty__icon"><i class="ph ph-folder-open" aria-hidden="true"></i></span><div><strong>还没有导入模型</strong><p>选择一个包含 Whisper 权重和配置文件的本机目录，系统会先做结构检查。</p></div></div>`;
  }
  const actionsRoot = $("#localModelsSettings");
  if (!actionsRoot) return;

  actionsRoot.querySelectorAll("[data-model-download]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await Api.downloadLocalModel(button.dataset.modelDownload);
    } catch (error) {
      button.disabled = false;
      actionFeedback(error.message || "下载官方模型失败", true);
      toast(error.message || "下载官方模型失败", "ph-warning-circle");
    } finally {
      await refresh({ force: true });
    }
  }));

  actionsRoot.querySelectorAll("[data-model-current]").forEach((button) => button.addEventListener("click", () => {
    document.dispatchEvent(new CustomEvent("whisper-model-select", { detail: { name: button.dataset.modelCurrent } }));
  }));

  actionsRoot.querySelectorAll("[data-model-check]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await Api.checkLocalModel(button.dataset.modelCheck);
      toast("模型检查通过", "ph-shield-check");
    } catch (error) {
      actionFeedback(error.message || "模型检查失败", true);
      toast(error.message || "模型检查失败", "ph-warning-circle");
    } finally {
      button.disabled = false;
      await refresh({ force: true });
    }
  }));

  actionsRoot.querySelectorAll("[data-model-delete]").forEach((button) => button.addEventListener("click", async () => {
    const name = button.dataset.modelDelete;
    if (currentWhisperModel && currentWhisperModel.replace(/^local:/, "") === name) {
      actionFeedback("当前模型正在使用，请先切换当前模型后再删除。", true);
      return;
    }
    if (!window.confirm(`删除 ${name} 并释放本地空间？`)) return;
    button.disabled = true;
    try {
      await Api.deleteLocalModel(name);
      toast("模型已删除", "ph-trash");
    } catch (error) {
      button.disabled = false;
      actionFeedback(error.message || "删除本地模型失败", true);
      toast(error.message || "删除本地模型失败", "ph-warning-circle");
    } finally {
      await refresh({ force: true });
    }
  }));
}

async function refresh({ force = false } = {}) {
  if (refreshInFlight) {
    if (force) forceRefreshQueued = true;
    return refreshInFlight;
  }
  refreshInFlight = (async () => {
    do {
      forceRefreshQueued = false;
      const generation = refreshGeneration;
      try {
        maxActiveRefreshes += 1;
        if (maxActiveRefreshes > 1) throw new Error("检测到重叠的本地模型刷新请求");
        const [items, settings] = await Promise.all([Api.listLocalModels(), Api.getModelSettings()]);
        if (generation !== refreshGeneration || !whisperPageActive || document.visibilityState !== "visible") continue;
        currentWhisperSettings = settings?.whisper || null;
        currentWhisperModel = currentWhisperSettings?.model || "";
        render(items);
        emitInventory(items);
        document.dispatchEvent(new CustomEvent("model-settings-change", { detail: { settings: { whisper: currentWhisperSettings }, source: "refresh" } }));
      } catch (error) {
        toast(error.message || "获取本地模型或设置失败", "ph-warning-circle");
      } finally {
        maxActiveRefreshes -= 1;
      }
    } while (forceRefreshQueued);
  })().finally(() => {
    refreshInFlight = null;
    if (forceRefreshQueued) void refresh({ force: true });
  });
  return refreshInFlight;
}

function existingModelNames() {
  return [...inventoryNames];
}

function setFeedback(message, state = "info", icon = "ph-info") {
  const feedback = $("#localModelImportFeedback");
  const status = $("#localModelImportStatus");
  const feedbackIcon = $("#localModelImportFeedbackIcon");
  if (!feedback || !status) return;
  feedback.hidden = !message;
  feedback.dataset.state = state;
  status.textContent = message;
  if (feedbackIcon) feedbackIcon.className = `ph ${icon}`;
}

function clearImportError() {
  if ($("#localModelImportFeedback")?.dataset.state === "error") setFeedback("");
}

function setFieldError(input, errorId, message = "") {
  const error = $(`#${errorId}`);
  if (!input || !error) return;
  input.setAttribute("aria-invalid", message ? "true" : "false");
  error.hidden = !message;
  error.textContent = message;
}

function renderFileSummary(analysis) {
  const summary = $("#localModelFileSummary");
  const title = $("#localModelFileSummaryTitle");
  const count = $("#localModelFileSummaryCount");
  const checks = $("#localModelFileChecks");
  const details = $("#localModelFileDetails");
  const fileList = $("#localModelFileList");
  const dropzone = $("#localModelDropzone");
  const dropzoneTitle = $("#localModelDropzoneTitle");
  const requirements = $("#localModelImportRequirements");
  if (!summary || !title || !count || !checks || !details || !fileList || !dropzone || !dropzoneTitle || !requirements) return;

  if (!selectedFiles.length) {
    summary.hidden = true;
    details.hidden = true;
    dropzone.dataset.state = "empty";
    dropzoneTitle.textContent = "拖入模型目录，或点击选择";
    requirements.textContent = "选择模型目录后将自动检查结构";
    return;
  }

  summary.hidden = false;
  title.textContent = analysis.directory;
  count.textContent = `${analysis.acceptedCount} 个可用文件${analysis.ignoredCount ? ` · 忽略 ${analysis.ignoredCount} 个` : ""}`;
  const checksData = [
    [analysis.checks.weights, `${analysis.weightCount} 个权重文件`],
    [analysis.checks.config, "config.json"],
    [analysis.checks.preprocessor, "preprocessor_config.json"],
    [analysis.checks.tokenizer, "分词器文件"],
  ];
  checks.innerHTML = checksData.map(([valid, label]) => `<span class="local-model-file-check local-model-file-check--${valid ? "ok" : "missing"}"><i class="ph ${valid ? "ph-check-circle" : "ph-warning-circle"}" aria-hidden="true"></i>${label}</span>`).join("");
  const accepted = analysis.files.slice(0, 30).map((file) => `<li><i class="ph ph-file" aria-hidden="true"></i><span>${escapeHtml(filePath(file))}</span></li>`);
  const ignored = analysis.ignored.slice(0, 10).map((file) => `<li class="is-ignored"><i class="ph ph-minus-circle" aria-hidden="true"></i><span>${escapeHtml(filePath(file))} · 不在支持范围</span></li>`);
  const missing = analysis.missing.map((item) => `<li class="is-missing"><i class="ph ph-warning" aria-hidden="true"></i><span>可能缺少：${escapeHtml(item)}</span></li>`);
  fileList.innerHTML = [...accepted, ...ignored, ...missing].join("") || "<li>没有可显示的文件详情</li>";
  details.hidden = false;
  dropzone.dataset.state = analysis.valid ? "valid" : "invalid";
  dropzoneTitle.textContent = analysis.valid ? `${analysis.directory} · 结构预检通过` : `${analysis.directory} · 还需要检查`;
  requirements.textContent = analysis.valid ? "结构预检通过，可以导入模型" : `还需处理 ${analysis.missing.length} 项结构检查`;
}

function updateImportPreview() {
  const form = $("#localModelImportForm");
  const submit = $("#localModelImportSubmit");
  const nameInput = $("#localModelName");
  const labelInput = $("#localModelLabel");
  if (!form || !submit || !nameInput || !labelInput) return;
  const analysis = analyzeModelFiles(selectedFiles);
  renderFileSummary(analysis);
  const nameValid = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(nameInput.value.trim());
  const labelValid = labelInput.value.trim().length > 0 && labelInput.value.trim().length <= 100;
  const valid = nameValid && labelValid && analysis.valid;
  submit.disabled = isImporting || !valid;
  form.dataset.ready = valid ? "true" : "false";
  if (!isImporting && !["success", "error"].includes($("#localModelImportFeedback")?.dataset.state)) {
    if (!selectedFiles.length) setFeedback("");
    else if (analysis.valid && valid) setFeedback("", "info");
  }
}

function validateImportForm() {
  const nameInput = $("#localModelName");
  const labelInput = $("#localModelLabel");
  const analysis = analyzeModelFiles(selectedFiles);
  let valid = true;
  if (!nameInput?.value.trim()) {
    setFieldError(nameInput, "localModelNameError", "请输入模型 ID。");
    valid = false;
  } else if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(nameInput.value.trim())) {
    setFieldError(nameInput, "localModelNameError", "模型 ID 需以字母或数字开头，只能包含字母、数字、下划线和短横线。");
    valid = false;
  } else setFieldError(nameInput, "localModelNameError");
  if (!labelInput?.value.trim()) {
    setFieldError(labelInput, "localModelLabelError", "请输入模型显示名称。");
    valid = false;
  } else setFieldError(labelInput, "localModelLabelError");
  if (!selectedFiles.length) {
    setFeedback("请选择一个模型目录后再导入。", "error", "ph-folder-open");
    valid = false;
  } else if (!analysis.valid) {
    setFeedback(`目录预检未通过：${analysis.missing.join("、")}。`, "error", "ph-warning-circle");
    valid = false;
  }
  return { valid, analysis };
}

function bindDropzone() {
  const dropzone = $("#localModelDropzone");
  const input = $("#localModelFiles");
  if (!dropzone || !input) return;
  const setFiles = (files) => {
    clearImportError();
    selectedFiles = Array.from(files || []);
    updateImportPreview();
  };
  input.addEventListener("change", () => setFiles(input.files));
  ["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  }));
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragging"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
    const files = event.dataTransfer?.files;
    if (!files?.length) {
      setFeedback("无法读取拖入内容，请使用“选择目录”打开文件夹。", "error", "ph-warning-circle");
      return;
    }
    setFiles(files);
  });
  dropzone.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    input.click();
  });
}

function bindNameSuggestion() {
  const nameInput = $("#localModelName");
  const labelInput = $("#localModelLabel");
  if (!nameInput || !labelInput) return;
  labelInput.addEventListener("input", () => {
    clearImportError();
    const current = nameInput.value.trim();
    if (!modelNameTouched || !current || current === lastSuggestedName) {
      lastSuggestedName = suggestModelId(labelInput.value, existingModelNames());
      nameInput.value = lastSuggestedName;
      modelNameTouched = false;
    }
    updateImportPreview();
  });
  nameInput.addEventListener("input", () => {
    clearImportError();
    modelNameTouched = nameInput.value.trim() !== lastSuggestedName;
    updateImportPreview();
  });
}

export function initLocalModels() {
  const form = $("#localModelImportForm");
  if (!form) return;
  bindDropzone();
  bindNameSuggestion();
  updateImportPreview();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = validateImportForm();
    if (!result.valid || isImporting) {
      updateImportPreview();
      return;
    }
    const name = $("#localModelName").value.trim();
    const label = $("#localModelLabel").value.trim();
    const submit = $("#localModelImportSubmit");
    isImporting = true;
    form.setAttribute("aria-busy", "true");
    submit.disabled = true;
    setFeedback("正在上传并验证模型，请稍候…", "busy", "ph-spinner");
    try {
      const item = await Api.importLocalModel(name, label, result.analysis.files);
      form.reset();
      selectedFiles = [];
      modelNameTouched = false;
      lastSuggestedName = "";
      setFeedback(`“${item.label || label}”已导入，可在任务创建中选择。`, "success", "ph-check-circle");
      toast("模型已导入并通过检查", "ph-shield-check");
    } catch (error) {
      setFeedback(error.message || "导入本地模型失败，请检查文件后重试。", "error", "ph-warning-circle");
    } finally {
      await refresh({ force: true });
      isImporting = false;
      form.removeAttribute("aria-busy");
      updateImportPreview();
    }
  });
  document.addEventListener("viewchange", (event) => {
    const visible = event.detail?.view === "model-settings"
      && !$("#model-panel-whisper")?.hidden
      && document.visibilityState === "visible";
    whisperPageActive = visible;
    refreshGeneration += 1;
    if (visible) {
      void refresh();
      syncPollTimer(true);
    } else syncPollTimer(false);
  });
  document.addEventListener("visibilitychange", () => {
    const visible = document.visibilityState === "visible" && document.body.dataset.route === "model-settings" && !$("#model-panel-whisper")?.hidden;
    whisperPageActive = visible;
    refreshGeneration += 1;
    if (visible) { void refresh(); syncPollTimer(true); }
    else syncPollTimer(false);
  });
  document.addEventListener("modelkindchange", (event) => {
    const visible = event.detail?.kind === "whisper" && document.body.dataset.route === "model-settings" && document.visibilityState === "visible";
    whisperPageActive = visible;
    refreshGeneration += 1;
    if (visible) {
      void refresh();
      syncPollTimer(true);
    } else syncPollTimer(false);
  });
  document.addEventListener("model-settings-change", (event) => {
    currentWhisperSettings = event.detail?.settings?.whisper || null;
    currentWhisperModel = currentWhisperSettings?.model || "";
    if (inventoryItems.length) render(inventoryItems);
    if (event.detail?.source === "mutation") {
      refreshGeneration += 1;
      void refresh({ force: true });
    }
  });
  if (document.body.dataset.route === "model-settings" && !$("#model-panel-whisper")?.hidden && document.visibilityState === "visible") {
    whisperPageActive = true;
    void refresh();
    syncPollTimer(true);
  }
}

export { render, refresh, validateImportForm };
