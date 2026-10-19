/* 入口：装配各模块并加载数据 */

import { isDesktop } from "./desktop.js";
import { mountDesktop } from "./desktop-ui.js";
import { bindDesktopQueue } from "./desktop-bridge.js";
import { toast } from "./toast.js";
import { initTheme } from "./theme.js";
import { initI18n } from "./i18n.js";
import { initShell } from "./ui-shell.js";
import { initConsole } from "./ui-console.js";
import { initQueue } from "./ui-queue.js";
import { initPreview } from "./ui-preview.js";
import { initEditor } from "./ui-editor.js";
import { initProbe } from "./ui-probe.js";
import { initStorage } from "./ui-storage.js";
import { initDrive } from "./ui-drive.js";
import { initTranslationSettings } from "./ui-translation-settings.js";
import { initAudioSettings } from "./ui-audio-settings.js";
import { initLocalModels } from "./ui-local-models.js";
import { initAdvancedSettings } from "./ui-advanced-settings.js";
import { initModelSettings } from "./ui-model-settings.js";
import { initPluginSettings } from "./ui-plugin-settings.js";
import { initRouter } from "./router.js";
import { loadTasks, stopAll, setView, setFilter } from "./store.js";
import { initDeveloperEntry } from "./developer-entry.js";
import { initSupportedSitesView } from "./supported-sites.js";
import { initApiConnection } from "./ui-api-connection.js";
import { initWorkbench } from "./workbench.js";
import { Api } from "./api.js";
import { consumeTrialTokenFragment } from "./api-auth.js";

consumeTrialTokenFragment(Api.base);
initApiConnection();
initI18n();
initTheme();
const desktopMode = isDesktop();
const desktop = desktopMode ? mountDesktop() : null;
if (!desktopMode) {
  initAdvancedSettings();
  initModelSettings();
}
initRouter();
if (!desktopMode) {
  initPluginSettings();
  initDeveloperEntry();
}
initShell();
if (desktopMode) {
  // Keep restored/deep-linked server settings out of the desktop primary path.
  const ensureDesktopView = () => {
    if (["mcp", "model-settings", "other-settings"].includes(document.body.dataset.route)) setView("tasks");
  };
  document.addEventListener("viewchange", ensureDesktopView);
  ensureDesktopView();
}
initConsole({ desktop: desktop?.controller });
if (desktop) bindDesktopQueue({ documentLike: document, windowLike: window,
  controller: desktop.controller, notify: (message) => toast(message, "ph-warning-circle") });
initQueue({ listSelector: "#taskList", origin: "web" });
if (!desktopMode) initQueue({ listSelector: "#mcpTaskList", origin: "mcp" });
initPreview();
initEditor();
initStorage();
if (!desktopMode) {
  initProbe();
  initDrive();
  initTranslationSettings();
  initAudioSettings();
  initLocalModels();
  initSupportedSitesView();
  initWorkbench();
}
desktop?.start();

loadTasks();

window.addEventListener("beforeunload", stopAll);

// 浏览器通知点击事件：切回任务视图，确保目标行在筛选范围内并滚到可视区。
window.addEventListener("subtrans:focus-task", (e) => {
  setView("tasks");
  setFilter("all");
  const id = e?.detail?.id;
  if (!id) return;
  // 等渲染完成再滚动；切回 tasks 视图会触发 ui-queue render。
  requestAnimationFrame(() => {
    const node = document.querySelector(`.qrow[data-id="${CSS.escape(id)}"]`);
    if (node && typeof node.scrollIntoView === "function") {
      node.scrollIntoView({ behavior: "smooth", block: "center" });
      node.classList.add("is-focus");
      setTimeout(() => node.classList.remove("is-focus"), 1600);
    }
  });
});
