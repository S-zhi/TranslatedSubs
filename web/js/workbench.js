/* Optional studio motion. Business controls never wait for the 3D renderer. */

const MOTION_KEY = "subtrans_motion";

export function initWorkbench({ loadScene = () => import("./workbench-scene.js") } = {}) {
  const host = document.querySelector("#studioVisual");
  const toggle = document.querySelector("#motionToggle");
  if (!host || !toggle) return () => {};

  const root = document.documentElement;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  let enabled = true;
  try { enabled = localStorage.getItem(MOTION_KEY) !== "off"; } catch (_) { /* private mode */ }
  let activeView = document.querySelector(".view.is-active")?.dataset.view;
  let inViewport = true;
  let suspended = false;
  let disposed = false;
  let loading = false;
  let failed = false;
  let scene = null;

  const isDark = () => root.dataset.theme === "dark"
    || (root.dataset.theme !== "light" && dark.matches);
  const visible = () => activeView === "tasks" && inViewport
    && document.visibilityState !== "hidden" && !suspended;

  function sync() {
    if (disposed) return;
    const motion = enabled && !reduced.matches;
    root.dataset.motion = motion ? "on" : "off";
    toggle.disabled = reduced.matches;
    toggle.setAttribute("aria-pressed", String(motion));
    const label = reduced.matches ? "系统已减少动态效果" : motion ? "暂停动效" : "开启动效";
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
    const icon = toggle.querySelector("i");
    if (icon) icon.className = `ph ${motion ? "ph-pause" : "ph-play"}`;

    if (scene) {
      scene.setTheme(isDark());
      scene.setMotionEnabled(motion && visible());
    } else if (visible() && !loading && !failed) {
      loading = true;
      // Defer the import until the workspace is visible; a failed optional effect
      // leaves the CSS illustration and every task control available.
      Promise.resolve().then(loadScene).then(({ createWorkbenchScene }) => {
        if (disposed) return;
        scene = createWorkbenchScene(host, { reducedMotion: false });
        sync();
      }).catch(() => {
        failed = true;
        scene?.dispose();
        scene = null;
        host.classList.remove("is-ready");
        host.classList.add("scene-unavailable");
      }).finally(() => { loading = false; });
    }
  }

  function onToggle() {
    enabled = !enabled;
    try { localStorage.setItem(MOTION_KEY, enabled ? "on" : "off"); } catch (_) { /* private mode */ }
    sync();
  }

  function onViewChange(event) {
    const nextView = event.detail?.view;
    // Store updates also emit viewchange; do not restart effects on each SSE tick.
    if (nextView === activeView) return;
    activeView = nextView;
    sync();
  }

  function onPageHide(event) {
    if (!event.persisted) { dispose(); return; }
    suspended = true;
    sync();
  }

  function onPageShow() {
    suspended = false;
    sync();
  }

  const visibilityObserver = typeof IntersectionObserver === "function"
    ? new IntersectionObserver(([entry]) => {
      inViewport = entry.isIntersecting;
      sync();
    }, { threshold: 0.05 }) : null;
  visibilityObserver?.observe(host);
  const themeObserver = new MutationObserver(() => {
    if (scene) scene.setTheme(isDark());
  });
  themeObserver.observe(root, { attributes: true, attributeFilter: ["data-theme"] });

  toggle.addEventListener("click", onToggle);
  reduced.addEventListener("change", sync);
  dark.addEventListener("change", sync);
  document.addEventListener("visibilitychange", sync);
  document.addEventListener("viewchange", onViewChange);
  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("pageshow", onPageShow);
  sync();

  function dispose() {
    if (disposed) return;
    disposed = true;
    visibilityObserver?.disconnect();
    themeObserver.disconnect();
    toggle.removeEventListener("click", onToggle);
    reduced.removeEventListener("change", sync);
    dark.removeEventListener("change", sync);
    document.removeEventListener("visibilitychange", sync);
    document.removeEventListener("viewchange", onViewChange);
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("pageshow", onPageShow);
    scene?.dispose();
    scene = null;
  }

  return dispose;
}
