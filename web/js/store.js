/* 应用状态 + 控制层（创建/删除/重试/SSE 跟踪），用极简发布订阅驱动各视图。 */

import { Api } from "./api.js";
import { TERMINAL } from "./constants.js";
import { createNotifications } from "./notifications.js";

export const state = {
  tasks: [],
  filter: "all",       // all | active | done | failed
  view: "tasks",       // tasks | preview | editor | storage | other-settings
  settingsTab: "engines", // engines | audio | probe | drive
  previewId: null,
  loading: true,
  loadError: null,
};

const listeners = new Set();
const subs = new Map(); // taskId -> unsubscribe

// 通知管理器：闭包持有节流/seen 状态，通过 dispatch 入口触发。
// 仅在浏览器环境下创建（window 存在）；SSR/测试环境可能没有 Notification。
const notifications = (typeof window !== "undefined")
  ? createNotifications({
      Notification: window.Notification,
      localStorage: window.localStorage,
      window,
    })
  : null;

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function emit(detail) {
  listeners.forEach((fn) => fn(detail || {}));
}

/* ---- 视图 / 筛选 / 预览选择 ---- */
export function setFilter(f) { state.filter = f; emit({ type: "filter" }); }
export function setView(v, options = {}) {
  if (!v || state.view === v) return;
  state.view = v;
  emit({ type: "view", history: options.history !== false });
}
export function setSettingsTab(tab) {
  if (!tab || state.settingsTab === tab) return;
  state.settingsTab = tab;
  emit({ type: "settings-tab", settingsTab: tab });
}
export function setPreviewId(id) { state.previewId = id; emit({ type: "preview" }); }

/* ---- 数据动作 ---- */
export async function loadTasks() {
  state.loading = true;
  state.loadError = null;
  emit({ type: "loading" });
  try {
    state.tasks = await Api.listTasks();
    state.loading = false;
    emit({ type: "loaded" });
    state.tasks.forEach(track);
  } catch (e) {
    state.loading = false;
    state.loadError = e.message || "无法连接后端";
    emit({ type: "loaderror" });
  }
}

export async function createTask(payload) {
  const t = payload.file
    ? await Api.createUploadTask(payload)
    : await Api.createTask(payload);
  state.tasks.unshift(t);
  state.filter = "all";
  emit({ type: "created", id: t.id });
  track(t);
  return t;
}

export async function deleteTask(id) {
  // 后端确认删除成功后再更新本地状态，避免失败时任务从界面消失。
  await Api.deleteTask(id);
  const u = subs.get(id);
  if (u) u();
  subs.delete(id);
  notifications?.forget(id);
  state.tasks = state.tasks.filter((t) => t.id !== id);
  if (state.previewId === id) state.previewId = null;
  emit({ type: "deleted", id });
}

export async function cancelTask(id) {
  const t = await Api.cancelTask(id);
  const u = subs.get(id);
  if (u) u();
  subs.delete(id);
  const i = state.tasks.findIndex((x) => x.id === id);
  if (i >= 0) state.tasks[i] = { ...state.tasks[i], ...t };
  emit({ type: "cancelled", id });
}

export async function retryTask(id) {
  const t = await Api.retryTask(id);
  const i = state.tasks.findIndex((x) => x.id === id);
  if (i >= 0) state.tasks[i] = { ...state.tasks[i], ...t };
  emit({ type: "retried", id });
  if (i >= 0) {
    // 重置通知节流窗口，让"任务重新开始"能被通知到。
    notifications?.forget(id);
    track(state.tasks[i]);
  }
}

/* ---- SSE 跟踪 ---- */
function track(t) {
  if (TERMINAL.has(t.status)) return;
  if (subs.has(t.id)) return;
  const unsub = Api.subscribeProgress(t.id, (update) => {
    const i = state.tasks.findIndex((x) => x.id === update.id);
    if (i < 0) return;
    const prevStatus = state.tasks[i].status;
    state.tasks[i] = { ...state.tasks[i], ...update };
    emit({ type: "progress", id: update.id, status: update.status });
    // 触发浏览器任务通知（受偏好 / 权限 / 节流门控）。
    if (notifications) {
      notifications.notify(state.tasks[i], prevStatus);
    }
    if (TERMINAL.has(update.status)) {
      const u = subs.get(update.id);
      if (u) u();
      subs.delete(update.id);
    }
  });
  subs.set(t.id, unsub);
}

export function stopAll() {
  subs.forEach((u) => u());
  subs.clear();
}

/* 通知模块：仅暴露给设置面板使用，避免视图层直接动 Notification。 */
export function notificationsApi() {
  return notifications;
}

/* ---- 选择器 ---- */
export function visibleTasks(origin = null) {
  const f = state.filter;
  return state.tasks.filter((t) => {
    if (origin && (t.taskOrigin || "web") !== origin) return false;
    if (f === "all") return true;
    if (f === "done") return t.status === "SUCCESS";
    if (f === "failed") return t.status === "FAILED" || t.status === "CANCELLED";
    if (f === "active") return !TERMINAL.has(t.status);
    return true;
  });
}

export function completedTasks() {
  return state.tasks.filter((t) => t.status === "SUCCESS");
}
