/* 入口：装配各模块并加载数据 */

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
import { initRouter } from "./router.js";
import { loadTasks, stopAll, setView, setFilter } from "./store.js";
import { initDeveloperEntry } from "./developer-entry.js";
import { initSupportedSitesView } from "./supported-sites.js";

initI18n();
initTheme();
initAdvancedSettings();
initModelSettings();
initRouter();
initDeveloperEntry();
initShell();
initConsole();
initQueue({ listSelector: "#taskList", origin: "web" });
initQueue({ listSelector: "#mcpTaskList", origin: "mcp" });
initPreview();
initEditor();
initProbe();
initStorage();
initDrive();
initTranslationSettings();
initAudioSettings();
initLocalModels();
initSupportedSitesView();

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
