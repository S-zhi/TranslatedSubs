import { createDesktopController, isDesktop } from "./desktop.js";
import { openDesktopUpdate, verifiedHttpsUrl } from "./desktop-bridge.js";

const PHASES = { not_installed: "等待准备", downloading: "下载中", converting: "转换中",
  checking: "校验中", ready: "已就绪", failed: "失败" };

export function formatDesktopBytes(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "大小未知";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function desktopModelProgress(model) {
  const phase = model.phase || model.status;
  const known = typeof model.total_bytes === "number" && Number.isFinite(model.total_bytes) && model.total_bytes > 0;
  const received = typeof model.downloaded_bytes === "number" && Number.isFinite(model.downloaded_bytes) && model.downloaded_bytes >= 0;
  return {
    phase: PHASES[phase] || PHASES[model.status] || "准备中",
    size: `${formatDesktopBytes(model.downloaded_bytes)} / ${formatDesktopBytes(model.total_bytes)}`,
    // Download bytes cannot describe conversion or validation progress.
    percent: phase === "downloading" && known && received
      ? Math.min(100, Math.max(0, model.downloaded_bytes / model.total_bytes * 100)) : null,
  };
}

export function applyDesktopDefaults(form, defaults, documentLike = globalThis.document) {
  if (!defaults) return;
  const labels = { sourceLang: "英语", targetLang: "简体中文", model: "本地英语转写", engine: "本地英译中" };
  for (const name of ["sourceLang", "targetLang", "model", "engine"]) {
    const select = form.elements[name];
    const option = documentLike.createElement("option");
    option.value = defaults[name];
    option.textContent = labels[name] || defaults[name];
    select.replaceChildren(option);
    select.value = defaults[name];
    select.disabled = true;
  }
  for (const name of ["mode", "burn", "needSubtitle"]) {
    const value = name === "needSubtitle" ? "on" : defaults[name];
    form.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
      input.checked = input.value === value;
      input.disabled = true;
    });
  }
  form.elements.ttsEnabled.checked = false;
  form.elements.ttsEnabled.disabled = true;
}

export function mountDesktop({ windowLike = globalThis.window, documentLike = globalThis.document,
  controller = createDesktopController() } = {}) {
  if (!isDesktop(windowLike)) return null;
  documentLike.body.classList.add("desktop-mode");
  const host = documentLike.querySelector("#desktopStatus");
  host.hidden = false;
  const make = (tag, className, text) => {
    const node = documentLike.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const heading = make("h2", "", "正在检查基础模型…");
  const message = make("p", "desktop-status__message");
  message.setAttribute("role", "status");
  const rows = make("div", "desktop-models");
  const retry = make("button", "btn btn--ghost btn--sm", "重试准备");
  retry.type = "button";
  retry.hidden = true;
  let retrying = false;
  retry.addEventListener("click", async () => {
    if (retrying) return;
    retrying = true;
    retry.disabled = true;
    try { await controller.retry(); } finally { retrying = false; retry.disabled = false; }
  });
  host.replaceChildren(heading, message, rows, retry);
  const update = documentLike.querySelector("#desktopUpdate");
  const updateTitle = make("strong", "");
  updateTitle.setAttribute("role", "status");
  const notes = make("p", "desktop-update__notes");
  const download = make("button", "btn btn--ghost btn--sm", "下载新版 DMG");
  download.type = "button";
  let updateUrl = null;
  download.addEventListener("click", () => {
    try {
      if (!openDesktopUpdate(updateUrl, windowLike)) notes.textContent = "无法打开下载链接，请在桌面应用中重试。";
    } catch (_) { notes.textContent = "无法打开浏览器，请稍后重试；视频任务不受影响。"; }
  });
  const updateRetry = make("button", "btn btn--ghost btn--sm", "重试检查更新");
  updateRetry.type = "button";
  updateRetry.hidden = true;
  let updateRetrying = false;
  updateRetry.addEventListener("click", async () => {
    if (updateRetrying) return;
    updateRetrying = true;
    updateRetry.disabled = true;
    try { await controller.checkUpdates({ force: true }); }
    finally { updateRetrying = false; updateRetry.disabled = false; }
  });
  update.replaceChildren(updateTitle, notes, download, updateRetry);
  const unsubscribe = controller.subscribe((state) => {
    heading.textContent = state.ready ? "基础模型已就绪" : state.error || state.models?.state === "failed" ? "模型准备需要处理" : "正在准备基础模型";
    message.textContent = state.error || (state.ready
      ? "可以开始处理英文视频。转写与翻译在本机完成。"
      : "首次使用需联网下载转写和翻译模型；准备完成后即可提交视频。关闭窗口后准备继续。");
    host.classList.toggle("is-ready", state.ready);
    host.classList.toggle("is-error", !!state.error || state.models?.state === "failed");
    retry.hidden = !state.error && state.models?.state !== "failed";
    rows.replaceChildren(...(state.models?.models || []).map((model) => {
      const progress = desktopModelProgress(model);
      const row = make("div", "desktop-model");
      row.append(make("strong", "", model.name || model.id), make("span", "", progress.phase),
        make("span", "desktop-model__size", progress.size));
      if (model.status !== "ready" && model.status !== "not_installed" && model.status !== "failed") {
        const bar = make("progress", "desktop-model__progress");
        bar.max = 100;
        bar.setAttribute("aria-label", `${model.name || model.id} · ${progress.phase}`);
        if (progress.percent !== null) bar.value = progress.percent;
        row.append(bar);
      }
      if (model.error) row.append(make("p", "desktop-model__error", model.error));
      return row;
    }));
    const result = state.updates;
    update.hidden = !result;
    updateRetry.hidden = result?.state !== "failed";
    if (result) {
      updateUrl = verifiedHttpsUrl(result.download_url);
      download.hidden = result.state !== "available" || !updateUrl;
      updateTitle.textContent = result.state === "available" ? `新版本 ${result.latest_version || ""} 可用`
        : result.state === "disabled" ? "尚未配置更新渠道"
          : result.state === "failed" ? "暂时无法检查更新"
            : result.state === "checking" ? "正在检查更新" : `已是最新版本 ${result.current_version || state.version || ""}`;
      notes.textContent = result.state === "available" ? result.release_notes || "暂无更新说明。"
        : result.state === "failed" ? "可继续处理视频。请检查网络后重试，也会在稍后自动检查。"
          : result.state === "disabled" ? "此版本尚未提供自动更新渠道，不影响视频处理。" : "";
    }
  });
  const stop = () => controller.stop();
  const start = () => { void controller.start(); };
  windowLike.addEventListener("pagehide", stop);
  windowLike.addEventListener("pageshow", start);
  return {
    controller,
    start,
    dispose() {
      unsubscribe(); controller.stop();
      windowLike.removeEventListener("pagehide", stop);
      windowLike.removeEventListener("pageshow", start);
    },
  };
}
