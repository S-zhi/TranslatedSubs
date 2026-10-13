/* 独立开发者页：所有状态只存在于当前页面内，不进入主应用 store 或本地存储。 */

import { createDeveloperState, visibleLogs } from "./developer-state.js";
import { initDeveloperSitesView } from "./developer-sites.js";
import { Api } from "./api.js";

const LOGS = [
  { time: "刚刚", level: "info", message: "Developer workspace initialized." },
  { time: "刚刚", level: "success", message: "Runtime log stream is ready." },
  { time: "刚刚", level: "info", message: "yt-dlp sites availability workbench mounted." },
  { time: "刚刚", level: "warn", message: "This page is intended for local debugging only." },
];

const state = createDeveloperState(LOGS);
const logRoot = document.querySelector("#developerLog");
const sessionRoot = document.querySelector("#developerSession");
const titleRoot = document.querySelector("#developerTitle");
const eyebrowRoot = document.querySelector(".developer-eyebrow");
const introDescRoot = document.querySelector(".developer-intro p");

const tabButtons = document.querySelectorAll(".developer-tab");
const logPanel = document.querySelector("#developerLogPanel");
const ytdlpPanel = document.querySelector("#developerYtdlpPanel");

export function switchTab(tabId) {
  const isYtdlp = tabId === "ytdlp";

  tabButtons.forEach((btn) => {
    const active = btn.dataset.tab === tabId;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });

  if (logPanel) logPanel.classList.toggle("is-hidden", isYtdlp);
  if (ytdlpPanel) ytdlpPanel.classList.toggle("is-hidden", !isYtdlp);

  if (titleRoot) {
    titleRoot.textContent = isYtdlp ? "yt-dlp 网站可用性" : "运行日志";
  }
  if (eyebrowRoot) {
    eyebrowRoot.textContent = isYtdlp ? "Extractor diagnostics" : "Runtime diagnostics";
  }
  if (introDescRoot) {
    introDescRoot.textContent = isYtdlp
      ? "针对主流视频网站进行 yt-dlp 可用性、提取器匹配及网络/反爬机制可视化诊断。"
      : "查看当前开发者页面的临时调试信息。刷新页面后，日志会重新从内存初始化。";
  }

  // 同步 URL 参数（不刷新页面）
  try {
    const url = new URL(window.location.href);
    if (isYtdlp) {
      url.searchParams.set("tab", "ytdlp");
    } else {
      url.searchParams.delete("tab");
    }
    window.history.replaceState({}, "", url.toString());
  } catch {}
}

function renderLogs() {
  if (!logRoot) return;
  const visible = visibleLogs(state);
  if (!visible.length) {
    logRoot.innerHTML = '<div class="developer-log__empty">暂无匹配的日志</div>';
    return;
  }
  logRoot.replaceChildren(...visible.map((item) => {
    const row = document.createElement("div");
    row.className = "developer-log__row";
    const time = document.createElement("span");
    time.className = "developer-log__time";
    time.textContent = item.time;
    const level = document.createElement("span");
    level.className = `developer-log__level developer-log__level--${item.level}`;
    level.textContent = item.level;
    const message = document.createElement("span");
    message.className = "developer-log__message";
    message.textContent = item.message;
    row.append(time, level, message);
    return row;
  }));
}

document.querySelectorAll("#developerLogPanel .developer-filter").forEach((button) => {
  button.addEventListener("click", () => {
    state.level = button.dataset.level;
    document.querySelectorAll("#developerLogPanel .developer-filter").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    renderLogs();
  });
});

document.querySelector("#developerClear")?.addEventListener("click", () => {
  state.logs = [];
  renderLogs();
});

document.querySelector("#developerBack")?.addEventListener("click", () => {
  window.location.assign("/tasks");
});

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    switchTab(btn.dataset.tab);
  });
});

if (sessionRoot) {
  sessionRoot.textContent = `session ${new Date().toISOString().slice(11, 19)} UTC`;
}

// 检查 URL 初始 Tab 参数
const initialTab = (function () {
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("tab") === "ytdlp") return "ytdlp";
    if (window.location.hash.includes("ytdlp")) return "ytdlp";
  } catch {}
  return "logs";
})();

if (initialTab === "ytdlp") {
  switchTab("ytdlp");
} else {
  renderLogs();
}

// 初始化站点测试视图
initDeveloperSitesView({ root: document, api: Api });
