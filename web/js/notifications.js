/* 浏览器任务通知工具：调度 + 节流 + 持久化偏好。
 *
 * 模块设计：
 *  - decideNotification(task, prevStatus) 是纯函数，无副作用，便于单元测试。
 *  - createNotifications(deps) 是工厂，注入 Notification / localStorage / window 等，
 *    实际打开通知、上次通知时间等都封装在闭包里。便于在测试里替换为 mock。
 *
 * 状态机：基于 store 的状态枚举，识别三类需要通知的转移：
 *   - PENDING -> 任意非 PENDING/终态：任务开始处理
 *   - 任意非 PENDING/终态 -> SUCCESS：处理完成
 *   - 任意非 PENDING/终态 -> FAILED：处理失败
 * 同一任务短时间内重复触发会被节流（throttleMs 默认 60s）。
 */

import { TERMINAL } from "./constants.js";
import { translate } from "./i18n.js";
import { shortUrl } from "./utils.js";

const STORAGE_KEY = "subtrans_notifications_enabled";
const DEFAULT_THROTTLE_MS = 60_000;
const DEFAULT_ENABLED = false;

function safeRead(localStorage) {
  try {
    return localStorage?.getItem(STORAGE_KEY);
  } catch (_) {
    return null;
  }
}

function safeWrite(localStorage, value) {
  try {
    localStorage?.setItem(STORAGE_KEY, value);
  } catch (_) {
    /* 忽略：隐私模式 / quota 异常时让偏好不持久化即可 */
  }
}

/* 根据 task 状态生成通知正文。纯函数：仅依赖入参。 */
function titleFor(task, kind) {
  if (kind === "success") return translate("任务已完成");
  if (kind === "failed") return translate("任务处理失败");
  return translate("任务已开始处理");
}

function bodyFor(task, kind) {
  const label = task.title || shortUrl(task.url || "") || task.id || "任务";
  if (kind === "failed") {
    const err = (task.error || "").trim();
    return err ? `${label} · ${err}` : label;
  }
  return label;
}

/* 纯函数：根据 (task, prevStatus) 判断是否应触发通知。
 * 返回 null 表示不发，返回 { kind, title, body } 表示建议触发的内容。
 * 节流 / 偏好 / 权限等副作用一律不在这里处理。
 */
export function decideNotification(task, prevStatus) {
  if (!task || !task.id || !task.status) return null;
  const next = task.status;
  if (next === prevStatus) return null;

  const isTerminal = TERMINAL.has(next);
  const isActive = !isTerminal && next !== "PENDING";
  const wasActive =
    !!prevStatus && !TERMINAL.has(prevStatus) && prevStatus !== "PENDING";

  let kind = null;
  if (next === "SUCCESS" && wasActive) kind = "success";
  else if (next === "FAILED" && wasActive) kind = "failed";
  else if (isActive && prevStatus === "PENDING") kind = "started";

  if (!kind) return null;
  return { kind, title: titleFor(task, kind), body: bodyFor(task, kind) };
}

/* 工厂：创建一个绑定到具体运行时（浏览器 / 测试 mock）的通知管理器。 */
export function createNotifications(deps) {
  const {
    Notification,
    localStorage,
    window,
    throttleMs = DEFAULT_THROTTLE_MS,
    defaultEnabled = DEFAULT_ENABLED,
    now = () => Date.now(),
  } = deps || {};

  // 同一任务的最近一次通知信息，用于节流。
  const lastNotified = new Map();
  // 已见过的任务 id：用于"首次 SSE 消息不通知"。
  // 任务进入终态或被删除时清理。
  const seen = new Set();
  // 偏好：默认关闭，用户主动开启时才弹权限请求并保存。
  let enabled =
    safeRead(localStorage) === "1" ? true : safeRead(localStorage) === "0" ? false : defaultEnabled;

  function isSupported() {
    return typeof Notification === "function" || (typeof Notification === "object" && Notification != null);
  }

  function getPermission() {
    if (!isSupported() || !Notification || typeof Notification.permission !== "string") {
      return "unsupported";
    }
    return Notification.permission;
  }

  function isEnabled() {
    return enabled;
  }

  async function setEnabled(next) {
    enabled = !!next;
    safeWrite(localStorage, next ? "1" : "0");
    if (next && getPermission() === "default" && typeof Notification?.requestPermission === "function") {
      try {
        await Notification.requestPermission();
      } catch (_) {
        /* 某些环境会抛错（隐私模式/旧版），忽略即可 */
      }
    }
  }

  function forget(taskId) {
    lastNotified.delete(taskId);
    seen.delete(taskId);
  }

  /* 主入口：在 store 监听到 SSE 状态变化时调用。
   * task: 更新后的 task 对象
   * prevStatus: 更新前的状态
   * opts.firstUpdate: 显式声明这是该任务的首条 SSE（页面刚加载时的回填）
   */
  function notify(task, prevStatus, opts = {}) {
    if (!enabled) return { fired: false, reason: "disabled" };
    if (!isSupported()) return { fired: false, reason: "unsupported" };
    if (getPermission() !== "granted") return { fired: false, reason: "denied" };

    const id = task?.id;
    if (!id) return { fired: false, reason: "no-id" };

    // 首次消息仅"认领"，不发通知：避免页面刚加载时把已经在跑的任务当作"开始"。
    // 注意：这里不要写 lastNotified，否则后续真正要发的通知会被节流窗口吞掉。
    if (opts.firstUpdate || !seen.has(id)) {
      seen.add(id);
      return { fired: false, reason: "first-update" };
    }

    const decision = decideNotification(task, prevStatus);
    if (!decision) return { fired: false, reason: "no-transition" };

    // 节流：同一任务、同一类通知在窗口内合并为一次；
    // 不同类（started / success / failed）之间不互斥，避免用户错过任务结果。
    const t = now();
    const last = lastNotified.get(id);
    if (last && last.kind === decision.kind && t - last.at < throttleMs) {
      lastNotified.set(id, { kind: decision.kind, at: t });
      return { fired: false, reason: "throttled" };
    }

    try {
      const n = new Notification(decision.title, {
        body: decision.body,
        tag: `subtrans-task-${id}`,
      });
      if (n && typeof n.addEventListener === "function" && typeof window?.focus === "function") {
        n.addEventListener("click", () => {
          try { window.focus(); } catch (_) {}
          // 触发自定义事件，让 store 切到任务视图
          try { window.dispatchEvent(new CustomEvent("subtrans:focus-task", { detail: { id } })); } catch (_) {}
        });
      }
    } catch (_) {
      // 通知构造失败不抛出，避免破坏 SSE 主流程
      return { fired: false, reason: "construct-failed" };
    }

    lastNotified.set(id, { kind: decision.kind, at: t });
    return { fired: true, kind: decision.kind };
  }

  function reset() {
    lastNotified.clear();
    seen.clear();
  }

  return {
    isSupported,
    getPermission,
    isEnabled,
    setEnabled,
    notify,
    forget,
    reset,
  };
}
