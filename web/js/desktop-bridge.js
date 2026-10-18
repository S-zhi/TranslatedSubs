import { isDesktop } from "./desktop.js";

export function verifiedHttpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname && !url.username && !url.password
      && url.href.length <= 2048 ? url.href : null;
  } catch (_) { return null; }
}

export function exportDesktopArtifact(taskId, kind, windowLike = globalThis.window) {
  const bridge = windowLike?.webkit?.messageHandlers?.desktop;
  if (!isDesktop(windowLike) || typeof bridge?.postMessage !== "function"
    || !["video", "subtitle"].includes(kind)
    || typeof taskId !== "string" || taskId.length > 128 || !/^[\w-]+$/.test(taskId)) return false;
  bridge.postMessage({ action: "export", taskId, kind });
  return true;
}

export function openDesktopUpdate(value, windowLike = globalThis.window) {
  const url = verifiedHttpsUrl(value);
  if (!url || !isDesktop(windowLike)) return false;
  const bridge = windowLike?.webkit?.messageHandlers?.desktop;
  if (typeof bridge?.postMessage !== "function") return false;
  bridge.postMessage({ action: "openExternal", url });
  return true;
}

// The queue owns its browser downloads. Capture only desktop actions, using the
// row's existing task ID (never titles, URLs or a local artifact path).
export function bindDesktopQueue({ documentLike, windowLike, controller, notify = () => {} }) {
  if (!isDesktop(windowLike)) return () => {};
  const onClick = (event) => {
    const button = event.target.closest?.("button");
    const row = button?.closest("#taskList .qrow[data-id]");
    if (!row) return;
    if (button.querySelector(".ph-arrow-clockwise") && !controller.snapshot.ready) {
      event.preventDefault(); event.stopImmediatePropagation();
      notify("基础模型尚未就绪，请先完成模型准备。");
      return;
    }
    const kind = button.querySelector(".ph-download-simple") ? "video"
      : button.querySelector(".ph-closed-captioning") ? "subtitle" : null;
    if (!kind) return;
    try {
      if (!exportDesktopArtifact(row.dataset.id, kind, windowLike)) return;
    } catch (_) { notify("无法打开保存对话框，请重试。"); }
    event.preventDefault(); event.stopImmediatePropagation();
  };
  documentLike.addEventListener("click", onClick, true);
  return () => documentLike.removeEventListener("click", onClick, true);
}
