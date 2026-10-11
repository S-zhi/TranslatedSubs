/* 浏览器路由：让每个工作区页面拥有稳定、可分享的 URL。 */

import { state, subscribe, setSettingsTab, setView } from "./store.js";

export const ROUTES = Object.freeze({
  tasks: { path: "/tasks", title: "任务" },
  mcp: { path: "/mcp", title: "MCP 调用" },
  preview: { path: "/preview", title: "视频预览" },
  editor: { path: "/editor", title: "字幕编辑" },
  storage: { path: "/storage", title: "本地资源" },
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
  return ROUTES[view]?.path || ROUTES.tasks.path;
}

function updateDocument(view) {
  const route = ROUTES[view] || ROUTES.tasks;
  document.title = `${route.title} · TranslatedSubs`;
  document.body.dataset.route = view;
}

export function initRouter() {
  const originalPath = window.location.pathname.replace(/\/$/, "") || "/tasks";
  const initialView = viewFromPath();
  if (window.location.pathname !== pathForView(initialView)) {
    window.history.replaceState({ view: initialView }, "", pathForView(initialView));
  }
  if (originalPath === "/probe") setSettingsTab("probe");
  if (originalPath === "/drive") setSettingsTab("drive");
  setView(initialView, { history: false });
  updateDocument(initialView);

  window.addEventListener("popstate", () => {
    const path = window.location.pathname;
    if (path === "/probe") setSettingsTab("probe");
    if (path === "/drive") setSettingsTab("drive");
    setView(viewFromPath(), { history: false });
  });

  subscribe(({ type, history }) => {
    if (type !== "view") return;
    const view = state.view;
    const path = pathForView(view);
    updateDocument(view);
    if (window.location.pathname !== path && history !== false) {
      window.history.pushState({ view }, "", path);
    }
  });
}
