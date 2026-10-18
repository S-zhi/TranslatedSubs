/* Desktop preparation state. No browser globals are read at import time. */
export function isDesktop(windowLike = globalThis.window) {
  return windowLike?.SUBTRANS_DESKTOP?.enabled === true;
}

export function desktopDefaults(defaults) {
  if (!defaults || defaults.sourceLang !== "en" || defaults.targetLang !== "zh-CN"
    || defaults.mode !== "bilingual" || defaults.burn !== "hard"
    || defaults.needSubtitle !== true || defaults.ttsEnabled !== false
    || !/^local:[\w.-]+$/.test(defaults.model || "")
    || defaults.engine !== "local-opus-en-zh") return null;
  // Only accept task fields, never configuration or credentials from a response.
  return Object.fromEntries(["sourceLang", "targetLang", "mode", "burn", "model",
    "engine", "needSubtitle", "ttsEnabled"].map((key) => [key, defaults[key]]));
}

export function desktopTaskPayload(payload, snapshot) {
  if (!snapshot?.ready || !desktopDefaults(snapshot.defaults)) {
    throw new Error("基础模型尚未就绪，请等待准备完成后再试。");
  }
  return { ...payload, ...desktopDefaults(snapshot.defaults), ttsVoice: "auto", originalVoiceMode: "keep" };
}

export function createDesktopController({
  fetchImpl = globalThis.fetch,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  pollMs = 1500,
  readyPollMs = 10000,
  updatePollMs = 1500,
  // Match the update service's default cache TTL. Non-forced reads still let
  // the backend own cache expiry, including cached failures.
  updateCheckMs = 3600000,
  timeoutMs = 15000,
} = {}) {
  let snapshot = { ready: false, defaults: null, models: null, error: null, updates: null };
  let active = false;
  let epoch = 0;
  let timer = null;
  let inFlight = null;
  let updateTimer = null;
  let updateInFlight = null;
  const requests = new Map();
  const listeners = new Set();
  const emit = (patch) => {
    snapshot = { ...snapshot, ...patch };
    listeners.forEach((listener) => listener(snapshot));
  };

  async function request(path, method = "GET") {
    const controller = new AbortController();
    const timeout = setTimer(() => controller.abort(), timeoutMs);
    requests.set(controller, timeout);
    try {
      // Relative URLs and HttpOnly cookies keep the native session out of JS.
      const response = await fetchImpl(path, {
        method, signal: controller.signal, credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`桌面服务请求失败（${response.status}），请重试。`);
      return await response.json();
    } finally {
      clearTimer(timeout);
      requests.delete(controller);
    }
  }

  function refresh({ retry = false } = {}) {
    if (!active) return Promise.resolve(snapshot);
    if (inFlight) return inFlight;
    clearTimer(timer);
    const generation = epoch;
    const current = () => active && generation === epoch;
    if (retry) emit({ ready: false, error: null });
    const work = async () => {
      try {
        if (retry) await request("/api/desktop/models/retry", "POST");
        if (!current()) return snapshot;
        let status = await request("/api/desktop/status");
        if (!current()) return snapshot;
        if (status.models?.state === "not_started") {
          emit({ ...status, defaults: desktopDefaults(status.defaults), ready: false, error: null });
          const models = await request("/api/desktop/models/prepare", "POST");
          if (!current()) return snapshot;
          status = { ...status, models, ready: false };
        }
        const defaults = desktopDefaults(status.defaults);
        let ready = status.ready === true && status.models?.ready === true
          && status.models?.state === "ready" && !!defaults;
        let error = status.models?.error || null;
        if (!defaults) error = "无法读取桌面默认参数，请重试或重新打开应用。";
        if (ready) {
          const inventory = await request("/api/srt/local-models");
          if (!current()) return snapshot;
          ready = Array.isArray(inventory) && inventory.some((item) =>
            `local:${String(item.name || "").replace(/^local:/, "")}` === defaults.model
            && String(item.status).toUpperCase() === "READY");
          if (!ready) error = "默认转写模型未在本地库存中就绪，请重试模型准备。";
        }
        emit({ ...status, defaults, ready, error });
      } catch (error) {
        if (current()) emit({ ready: false, error: error.name === "AbortError"
          ? "桌面服务响应超时，将自动重连；也可以重试。"
          : error.message || "无法连接桌面服务，将自动重连。" });
      }
      return snapshot;
    };
    inFlight = work().finally(() => {
      if (!current()) return;
      inFlight = null;
      timer = setTimer(() => { void refresh(); }, snapshot.ready ? readyPollMs : pollMs);
    });
    return inFlight;
  }

  function checkUpdates({ force = false } = {}) {
    if (!active) return Promise.resolve(snapshot.updates);
    if (updateInFlight) return updateInFlight;
    clearTimer(updateTimer);
    const generation = epoch;
    const current = () => active && generation === epoch;
    emit({ updates: { ...snapshot.updates, state: "checking", error: null } });
    const work = async () => {
      try {
        const updates = await request(`/api/desktop/updates?force=${force}`);
        if (current()) emit({ updates });
      } catch (_) {
        if (current()) emit({ updates: { state: "failed", error: "更新检查暂时不可用，不影响视频处理。" } });
      }
      return snapshot.updates;
    };
    updateInFlight = work().finally(() => {
      if (!current()) return;
      updateInFlight = null;
      // A check starts asynchronous backend work; keep reading until terminal.
      // Disabled channels cannot change until the backend restarts.
      if (snapshot.updates?.state === "disabled") return;
      updateTimer = setTimer(() => {
        if (current()) void checkUpdates();
      }, snapshot.updates?.state === "checking" ? updatePollMs : updateCheckMs);
    });
    return updateInFlight;
  }

  return {
    get snapshot() { return snapshot; },
    subscribe(listener) { listeners.add(listener); listener(snapshot); return () => listeners.delete(listener); },
    start() {
      if (active) return inFlight || Promise.resolve(snapshot);
      active = true;
      epoch += 1;
      emit({ ready: false, error: null, updates: null });
      void checkUpdates();
      return refresh();
    },
    refresh,
    checkUpdates,
    async retry() {
      const generation = epoch;
      if (inFlight) await inFlight;
      if (!active || generation !== epoch) return snapshot;
      return refresh({ retry: true });
    },
    stop() {
      active = false;
      epoch += 1;
      clearTimer(timer);
      clearTimer(updateTimer);
      requests.forEach((timeout, controller) => {
        clearTimer(timeout);
        controller.abort();
      });
      requests.clear();
      inFlight = null;
      updateInFlight = null;
      emit({ ready: false });
    },
  };
}
