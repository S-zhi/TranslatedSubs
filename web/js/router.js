/* 浏览器路由：让每个工作区页面拥有稳定、可分享的 URL。 */

import { state, subscribe, setSettingsTab, setView, setModelKind } from "./store.js";

export const ROUTES = Object.freeze({
  tasks: { path: "/tasks", title: "任务" },
  mcp: { path: "/mcp", title: "MCP 调用" },
  preview: { path: "/preview", title: "视频预览" },
  editor: { path: "/editor", title: "字幕编辑" },
  storage: { path: "/storage", title: "本地资源" },
  "model-settings": { path: "/model-settings", title: "模型设置" },
  "other-settings": { path: "/settings", title: "其他设置" },
});

const VIEW_BY_PATH = Object.freeze(
  Object.fromEntries(Object.entries(ROUTES).map(([view, route]) => [route.path, view])),
);

export function viewFromPath(pathname = window.location.pathname) {
  const path = pathname.replace(/\/$/, "") || "/tasks";
  if (["/probe", "/drive", "/settings"].includes(path)) return "other-settings";
  return VIEW_BY_PATH[path] || "tasks";
}

export function pathForView(view) {
  const path = ROUTES[view]?.path || ROUTES.tasks.path;
  if (view === "model-settings" && state.modelKind !== "llm") return `${path}?kind=${state.modelKind}`;
  if (view === "other-settings" && SETTINGS_TABS.has(state.settingsTab) && state.settingsTab !== "engines") return `${path}?tab=${state.settingsTab}`;
  return path;
}

const SETTINGS_TABS = new Set(["engines", "audio", "probe", "drive", "plugins"]);

function settingsTabFromLocation() {
  const path = window.location.pathname.replace(/\/$/, "");
  if (path === "/probe") return "probe";
  if (path === "/drive") return "drive";
  const tab = new URLSearchParams(window.location.search).get("tab");
  return SETTINGS_TABS.has(tab) ? tab : "engines";
}

function modelKindFromLocation() {
  const kind = new URLSearchParams(window.location.search).get("kind");
  return ["llm", "whisper", "tts"].includes(kind) ? kind : "llm";
}

function updateDocument(view) {
  const route = ROUTES[view] || ROUTES.tasks;
  document.title = `${route.title} · TranslatedSubs`;
  document.body.dataset.route = view;
}

export function initRouter() {
  const initialView = viewFromPath();
  setModelKind(modelKindFromLocation(), { history: false });
  if (initialView === "other-settings") setSettingsTab(settingsTabFromLocation(), { history: false });
  if (window.location.pathname !== pathForView(initialView)) {
    window.history.replaceState({ view: initialView }, "", pathForView(initialView));
  }
  setView(initialView, { history: false });
  updateDocument(initialView);

  window.addEventListener("popstate", () => {
    setModelKind(modelKindFromLocation(), { history: false });
    if (viewFromPath() === "other-settings") setSettingsTab(settingsTabFromLocation(), { history: false });
    setView(viewFromPath(), { history: false });
    updateDocument(state.view);
  });

  subscribe(({ type, history }) => {
    if (!["view", "model-kind", "legacy-model-navigation", "settings-tab"].includes(type)) return;
    const view = state.view;
    const path = pathForView(view);
    updateDocument(view);
    if (`${window.location.pathname}${window.location.search}` !== path && history !== false) {
      window.history.pushState({ view }, "", path);
    }
  });

  document.addEventListener("viewchange", (event) => {
    if (event.detail?.settingsTab === "models" && (state.view !== "model-settings" || state.modelKind !== "whisper")) {
      setSettingsTab("models");
    }
  });
  document.addEventListener("modelkindrequest", (event) => {
    const kind = event.detail?.kind;
    if (!kind || !["llm", "whisper", "tts"].includes(kind)) return;
    if (state.view !== "model-settings") setView("model-settings");
    setModelKind(kind);
  });
}
