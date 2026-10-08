/* 数据层：真实接口（REST + SSE）与 mock，按 config 切换。契约与后端一致。 */

import { TERMINAL, LANG_LABEL } from "./constants.js";
import { DEFAULT_SITES } from "./developer-sites-data.js";
import { uid, clamp, shortUrl, statusForProgress } from "./utils.js";

const CFG = (typeof window !== "undefined" && window.APP_CONFIG) || {
  API_BASE_URL:
    typeof window !== "undefined" && window.location && window.location.origin
      ? window.location.origin
      : "http://localhost:8000",
  USE_MOCK: false,
  API_TIMEOUT_MS: 15000,
};
export const USE_MOCK = Boolean(CFG.USE_MOCK);

const BROWSER_COOKIE_SOURCES = new Set([
  "chrome",
  "chromium",
  "edge",
  "firefox",
  "brave",
  "vivaldi",
  "opera",
  "safari",
]);

function probeRequestBody(url, options = {}) {
  const body = { url };
  const browser = options && typeof options === "object"
    ? options.cookiesFromBrowser
    : null;
  if (typeof browser === "string" && BROWSER_COOKIE_SOURCES.has(browser)) {
    body.cookiesFromBrowser = browser;
  }
  return body;
}

// 为普通 REST 请求统一接入超时控制；SSE 订阅保留独立连接策略。
async function request(base, path, options = {}) {
  const timeoutMs = Number(CFG.API_TIMEOUT_MS) || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${base}${path}`, {
      ...options,
      signal: controller.signal,
    });
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error("连接后端超时，请检查 FastAPI 是否启动");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function readError(res, fallback) {
  let detail = "";
  try {
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      if (data && data.detail) {
        if (Array.isArray(data.detail)) {
          detail = data.detail.map((item) => `${item.loc?.join(".")}: ${item.msg}`).join("; ");
        } else if (typeof data.detail === "object") {
          const message = data.detail.message || data.detail.code || "请求失败";
          const suggestion = data.detail.suggestion;
          detail = suggestion ? `${message}；${suggestion}` : String(message);
        } else {
          detail = String(data.detail);
        }
      } else {
        detail = text || String(res.status);
      }
    } catch (e) {
      detail = text || String(res.status);
    }
  } catch (e) {
    detail = String(res.status);
  }
  if (!detail || !detail.trim()) {
    detail = String(res.status);
  }
  return `${fallback}：${detail}`;
}

const RealApi = {
  base: CFG.API_BASE_URL,

  async createTask(payload) {
    const res = await request(this.base, "/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "创建任务失败"));
    return res.json();
  },

  // 上传本地视频并创建后续字幕处理任务。
  async createUploadTask(payload) {
    if (!(payload.file instanceof File)) {
      throw new Error("请先选择本地视频文件");
    }
    const body = new FormData();
    body.append("file", payload.file, payload.file.name);
    body.append("sourceLang", payload.sourceLang);
    body.append("targetLang", payload.targetLang);
    body.append("mode", payload.mode);
    body.append("burn", payload.burn);
    body.append("model", payload.model);
    body.append("engine", payload.engine);
    body.append("needSubtitle", String(payload.needSubtitle));
    body.append("ttsEnabled", String(payload.ttsEnabled));
    body.append("ttsVoice", payload.ttsVoice || "auto");
    body.append("originalVoiceMode", payload.originalVoiceMode || "keep");

    const res = await request(this.base, "/api/tasks/upload", {
      method: "POST",
      body,
    });
    if (!res.ok) throw new Error(await readError(res, "上传任务创建失败"));
    return res.json();
  },

  // 探测链接是否能被 yt-dlp 解析并找到可下载格式。
  async probeVideo(url, options = {}) {
    const res = await request(this.base, "/api/tasks/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(probeRequestBody(url, options)),
    });
    if (!res.ok) throw new Error(await readError(res, "链接校验失败"));
    return res.json();
  },

  // 获取 yt-dlp 运行时版本与提取器环境信息。
  async getYtDlpInfo() {
    const res = await request(this.base, "/api/tasks/probe/ytdlp-info");
    if (!res.ok) throw new Error(await readError(res, "获取 yt-dlp 环境信息失败"));
    return res.json();
  },

  // 列出最近的下载测试历史记录（按时间倒序）。
  async listProbeRecords(limit = 50) {
    const res = await request(
      this.base,
      `/api/tasks/probe/records?limit=${encodeURIComponent(limit)}`,
    );
    if (!res.ok) throw new Error(await readError(res, "获取测试历史失败"));
    return res.json();
  },

  // 获取固定十站点启动探测与开发者页重测的实时状态。
  async getProbeStartupStatus() {
    const res = await request(this.base, "/api/tasks/probe/startup-status");
    if (!res.ok) throw new Error(await readError(res, "获取站点可用性状态失败"));
    return res.json();
  },

  // 一键清空所有下载测试历史。
  async clearProbeRecords() {
    const res = await request(this.base, "/api/tasks/probe/records", {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(await readError(res, "清空测试历史失败"));
    return res.json();
  },

  // 删除单条下载测试历史。
  async deleteProbeRecord(id) {
    const res = await request(this.base, `/api/tasks/probe/records/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(await readError(res, "删除测试记录失败"));
  },

  async listTasks() {
    const res = await request(this.base, "/api/tasks");
    if (!res.ok) throw new Error(await readError(res, "获取任务列表失败"));
    return res.json();
  },

  // 获取源视频语言选项。
  async listVideoLanguages() {
    const res = await request(this.base, "/api/srt/languages");
    if (!res.ok) throw new Error(await readError(res, "获取源语言失败"));
    return res.json();
  },

  // 获取目标视频语言选项。
  async listTargetLanguages() {
    const res = await request(this.base, "/api/srt/target-languages");
    if (!res.ok) throw new Error("获取目标语言失败：" + res.status);
    return res.json();
  },

  // 获取 Whisper 模型权重选项。
  async listModelWeights() {
    const res = await request(this.base, "/api/srt/model-options");
    if (!res.ok) throw new Error(await readError(res, "获取模型列表失败"));
    return res.json();
  },

  async listLocalModels() {
    const res = await request(this.base, "/api/srt/local-models");
    if (!res.ok) throw new Error(await readError(res, "获取本地模型失败"));
    return res.json();
  },

  async downloadLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}/download`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "下载官方模型失败"));
    return res.json();
  },

  async importLocalModel(name, label, files) {
    const body = new FormData();
    body.append("name", name);
    body.append("label", label);
    for (const file of files) body.append("files", file, file.name);
    const res = await fetch(`${this.base}/api/srt/local-models/import`, { method: "POST", body });
    if (!res.ok) throw new Error(await readError(res, "导入本地模型失败"));
    return res.json();
  },

  async checkLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}/check`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "检查本地模型失败"));
    return res.json();
  },

  async deleteLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await readError(res, "删除本地模型失败"));
    return res.json();
  },

  async getReplicateBalance() {
    const res = await request(this.base, "/api/replicate/balance");
    if (!res.ok) throw new Error(await readError(res, "获取 Replicate 账户状态失败"));
    return res.json();
  },

  async listTranslationEngines() {
    const res = await request(this.base, "/api/settings/translation-engines");
    if (!res.ok) throw new Error(await readError(res, "获取翻译引擎失败"));
    return res.json();
  },

  async getAudioSettings() {
    const res = await request(this.base, "/api/settings/audio");
    if (!res.ok) throw new Error(await readError(res, "读取音频设置失败"));
    return res.json();
  },

  async updateAudioSettings(payload) {
    const res = await request(this.base, "/api/settings/audio", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存音频设置失败"));
    return res.json();
  },

  async getReplicateSettings() {
    const res = await request(this.base, "/api/settings/replicate");
    if (!res.ok) throw new Error(await readError(res, "读取 Replicate 设置失败"));
    return res.json();
  },

  async updateReplicateSettings(payload) {
    const res = await request(this.base, "/api/settings/replicate", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存 Replicate 设置失败"));
    return res.json();
  },

  async createTranslationEngine(payload) {
    const res = await request(this.base, "/api/settings/translation-engines", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "创建翻译引擎失败"));
    return res.json();
  },

  async updateTranslationEngine(id, payload) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存翻译引擎失败"));
    return res.json();
  },

  async validateTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}/validate`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(await readError(res, "检测翻译引擎失败"));
    return res.json();
  },

  async deleteTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await readError(res, "删除翻译引擎失败"));
  },

  async deleteTask(id) {
    const res = await request(this.base, `/api/tasks/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await readError(res, "删除失败"));
  },

  async cancelTask(id) {
    const res = await request(this.base, `/api/tasks/${id}/cancel`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "取消失败"));
    return res.json();
  },

  async retryTask(id) {
    const res = await request(this.base, `/api/tasks/${id}/retry`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "重试失败"));
    return res.json();
  },

  // 请求后端打开任务所在的本地文件夹。
  async openFolder(id) {
    const res = await request(this.base, `/api/tasks/${id}/folder`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "打开文件夹失败"));
  },

  async folderCapability(id) {
    const res = await request(this.base, `/api/tasks/${encodeURIComponent(id)}/folder-capability`);
    if (!res.ok) throw new Error(await readError(res, "查询文件夹能力失败"));
    return res.json();
  },

  async listTaskFiles(id, path = "") {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const res = await request(this.base, `/api/tasks/${encodeURIComponent(id)}/files${query}`);
    if (!res.ok) throw new Error(await readError(res, "读取任务文件失败"));
    return res.json();
  },

  taskFileUrl(id, path) {
    return `${this.base}/api/tasks/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`;
  },

  // ---------- 本地资源治理 ----------
  async startDriveUpload(taskId, artifactNames = []) {
    const res = await request(this.base, `/api/storage/tasks/${encodeURIComponent(taskId)}/drive/upload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(artifactNames.length ? { artifactNames } : {}),
    });
    if (!res.ok) throw new Error(await readError(res, "创建 Drive 上传批次失败"));
    return res.json();
  },

  async startDriveDownload(taskId) {
    const res = await request(this.base, `/api/storage/tasks/${encodeURIComponent(taskId)}/drive/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(await readError(res, "创建 Drive 下载批次失败"));
    return res.json();
  },

  async getDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}`);
    if (!res.ok) throw new Error(await readError(res, "读取 Drive 批次失败"));
    return res.json();
  },

  async retryDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}/retry`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "重试 Drive 批次失败"));
    return res.json();
  },

  async cancelDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}/cancel`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "取消 Drive 批次失败"));
    return res.json();
  },

  async getStorageStats() {
    const res = await request(this.base, "/api/storage/stats");
    if (!res.ok) throw new Error("获取存储统计失败：" + res.status);
    return res.json();
  },

  async previewCleanup(payload) {
    const res = await request(this.base, "/api/storage/cleanup_preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error(await readError(res, "预览清理失败"));
    return res.json();
  },

  async runCleanup(payload) {
    const res = await request(this.base, "/api/storage/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error(await readError(res, "执行清理失败"));
    return res.json();
  },

  async getRetention() {
    const res = await request(this.base, "/api/storage/retention");
    if (!res.ok) throw new Error(await readError(res, "获取保留策略失败"));
    return res.json();
  },

  async putRetention(days) {
    const res = await request(this.base, "/api/storage/retention", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days }),
    });
    if (!res.ok) throw new Error(await readError(res, "保存保留策略失败"));
    return res.json();
  },

  // 拉取任务 current subtitles（original + translated），解析为前端可编辑结构
  async getSubtitles(id) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`);
    if (!res.ok) throw new Error(await readError(res, "读取字幕失败"));
    return res.json();
  },

  // 保存编辑后的字幕到后端；version 为可选版本号（例 "v2"）
  async saveSubtitles(id, payload) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存字幕失败"));
    return res.json();
  },

  // 基于当前 translated.srt 重新烧录成品；mode 可选覆盖任务设置
  async reburnSubtitles(id, payload = {}) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles/burn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "重新烧录失败"));
    return res.json();
  },

  // SSE 订阅单任务进度（含自动重连、退避策略与超时断流捕获），返回取消函数
  subscribeProgress(id, onUpdate) {
    let es = null;
    let retryCount = 0;
    let reconnectTimer = null;
    let isClosed = false;

    const connect = () => {
      if (isClosed) return;
      es = new EventSource(`${this.base}/api/tasks/${id}/stream`);

      const handlePayload = (data) => {
        if (!data) return;
        if (data.status && TERMINAL.has(data.status)) {
          isClosed = true;
          if (es) es.close();
        }
        onUpdate({ ...data, _streamStatus: "connected" });
      };

      es.onmessage = (e) => {
        retryCount = 0;
        try {
          const data = JSON.parse(e.data);
          handlePayload(data);
        } catch (_) {}
      };

      es.addEventListener("end", (e) => {
        retryCount = 0;
        isClosed = true;
        if (es) es.close();
        try {
          if (e.data) {
            const data = JSON.parse(e.data);
            handlePayload(data);
          }
        } catch (_) {}
      });

      es.addEventListener("timeout", (e) => {
        isClosed = true;
        if (es) es.close();
        try {
          const data = JSON.parse(e.data);
          onUpdate({ id, _streamStatus: "timeout", error: data?.error || "连接已超时" });
        } catch (_) {
          onUpdate({ id, _streamStatus: "timeout", error: "连接已超时" });
        }
      });

      es.onerror = () => {
        if (isClosed) return;
        if (es) es.close();
        retryCount++;
        const delay = Math.min(1000 * Math.pow(2, retryCount - 1), 30000);
        onUpdate({ id, _streamStatus: "reconnecting", _retryCount: retryCount });
        reconnectTimer = setTimeout(() => {
          if (!isClosed) connect();
        }, delay);
      };
    };

    connect();

    return () => {
      isClosed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (es) es.close();
    };
  },

  downloadUrl(id, kind) {
    return kind === "subtitle"
      ? `${this.base}/api/tasks/${id}/subtitle`
      : kind === "dubbed"
        ? `${this.base}/api/tasks/${id}/dubbed`
      : kind === "source"
        ? `${this.base}/api/tasks/${id}/source`
      : `${this.base}/api/tasks/${id}/download`;
  },
};

const MockApi = (() => {
  const STORE_KEY = "subtrans_mock_tasks_v1";
  // 下载测试历史：与真实后端的 probe_records 行为对齐
  const PROBE_STORE_KEY = "subtrans_mock_probe_records_v1";

  // 加载 / 持久化历史记录的辅助
  function _loadProbe() {
    try {
      const raw = localStorage.getItem(PROBE_STORE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }
  function _saveProbe(arr) {
    try { localStorage.setItem(PROBE_STORE_KEY, JSON.stringify(arr)); } catch (e) {}
  }
  function _pushProbe(rec) {
    const arr = _loadProbe();
    arr.unshift(rec);
    // 简单上限：保留最近 100 条，避免 localStorage 膨胀
    if (arr.length > 100) arr.length = 100;
    _saveProbe(arr);
  }

  // 按 taskId 缓存 in-memory subtitles（编辑/保存即时反馈）
  const _subtitles = {};

  function _seedSubs(t) {
    const make = (origs, trans) => ({
      hasOriginal: !!origs, hasTranslated: !!trans,
      original: origs || [],
      translated: trans || [],
    });
    if (t.status !== "SUCCESS") return make(null, null);
    const orig = [
      { id: "sub_demo1", index: 1, start: 0, end: 2.4, text: "Welcome to the demo." },
      { id: "sub_demo2", index: 2, start: 2.4, end: 5.0, text: "Today we'll learn about subtitle editing." },
      { id: "sub_demo3", index: 3, start: 5.0, end: 7.8, text: "You can adjust timing, text, and split cues." },
    ];
    const tra = [
      { id: "sub_demo1", index: 1, start: 0, end: 2.4, text: "欢迎使用示例。" },
      { id: "sub_demo2", index: 2, start: 2.4, end: 5.0, text: "今天我们来看看字幕编辑。" },
      { id: "sub_demo3", index: 3, start: 5.0, end: 7.8, text: "你可以调整时间、文本，或者拆分合并。" },
    ];
    return make(orig, tra);
  }

  function seed() {
    const now = Date.now();
    return [
      {
        id: uid(), url: "https://example.com/watch?v=demo-finished", title: "示例视频 · 已完成",
        sourceLang: "en", targetLang: "zh-CN", mode: "bilingual", burn: "hard", model: "local:tiny",
        engine: "deepseek", status: "SUCCESS", progress: 100, error: null,
        createdAt: now - 1000 * 60 * 42, outputs: { video: "#", subtitle: "#" }, _sim: false,
      },
      {
        id: uid(), url: "https://example.com/watch?v=demo-running", title: null,
        sourceLang: "auto", targetLang: "zh-CN", mode: "mono", burn: "hard", model: "local:tiny",
        engine: "deepseek", status: "TRANSCRIBING", progress: 48, error: null,
        createdAt: now - 1000 * 90, outputs: null, _sim: true,
      },
      {
        id: uid(), url: "https://example.com/watch?v=demo-failed", title: null,
        sourceLang: "auto", targetLang: "ja", mode: "mono", burn: "soft", model: "medium",
        engine: "deepseek", status: "FAILED", progress: 22,
        error: "未设置 REPLICATE_API_TOKEN（请在 .env 中配置）",
        createdAt: now - 1000 * 60 * 8, outputs: null, _sim: false,
      },
    ];
  }

  let tasks;
  try {
    const raw = localStorage.getItem(STORE_KEY);
    tasks = raw ? JSON.parse(raw) : seed();
  } catch (e) {
    tasks = seed();
  }
  const persist = () => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(tasks)); } catch (e) {}
  };
  const find = (id) => tasks.find((t) => t.id === id);
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  return {
    async createTask(payload) {
      const t = {
        id: uid(), url: payload.url, title: null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model: payload.model, engine: payload.engine,
        status: "PENDING", progress: 0, error: null, createdAt: Date.now(), outputs: null, _sim: true,
      };
      tasks.unshift(t); persist(); await delay(150); return { ...t };
    },
    // 示例模式下模拟本地视频上传任务。
    async createUploadTask(payload) {
      const t = {
        id: uid(), url: payload.file?.name || "uploaded-video", title: payload.file?.name || null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model: payload.model, engine: payload.engine,
        sourceType: "upload", needSubtitle: payload.needSubtitle,
        status: "PENDING", progress: 0, error: null, createdAt: Date.now(), outputs: null, _sim: true,
      };
      tasks.unshift(t); persist(); await delay(180); return { ...t };
    },
    // 示例模式下模拟链接探测成功，同时写入历史。
    async probeVideo(url, options = {}) {
      void options;
      await delay(180);
      const ok = /^https?:\/\/.+/i.test(url);
      const result = {
        ok,
        title: ok ? "示例视频 · " + shortUrl(url) : null,
        extractor: ok ? "Mock" : null,
        duration: ok ? 90 : null,
        formatsCount: ok ? 3 : 0,
        webpageUrl: url,
        reason: ok ? null : "请输入有效的视频链接",
        detail: null,
      };
      _pushProbe({
        id: "probe_" + Math.random().toString(16).slice(2, 10),
        url,
        ok,
        title: result.title,
        extractor: result.extractor,
        duration: result.duration,
        formatsCount: result.formatsCount,
        webpageUrl: result.webpageUrl,
        reason: result.reason,
        detail: result.detail,
        createdAt: Date.now(),
      });
      return result;
    },
    async getYtDlpInfo() {
      await delay(40);
      return {
        version: "2026.06.09",
        extractorsCount: 1747,
        proxyConfigured: false,
        proxyMasked: null,
        cookiesConfigured: false,
        browserCookiesEnabled: false,
        cacheTtlSec: 300,
      };
    },
    // 示例模式下：返回历史记录（按时间倒序，遵守 limit）。
    async listProbeRecords(limit = 50) {
      await delay(60);
      const arr = _loadProbe();
      const n = Math.max(1, Math.min(500, Number(limit) || 50));
      return arr.slice(0, n);
    },
    async getProbeStartupStatus() {
      await delay(20);
      const now = Date.now();
      return {
        runId: "mock_startup",
        state: "completed",
        total: DEFAULT_SITES.length,
        completed: DEFAULT_SITES.length,
        successful: DEFAULT_SITES.length,
        failed: 0,
        runStartedAt: now,
        updatedAt: now,
        sites: DEFAULT_SITES.map((site) => ({
          id: site.id,
          name: site.name,
          domain: site.domain,
          url: site.url,
          status: "ok",
          source: "startup",
          updatedAt: now,
          result: {
            ok: true,
            title: `示例视频 · ${site.name}`,
            extractor: site.extractor,
            formatsCount: 3,
            webpageUrl: site.url,
            reason: null,
            detail: null,
            availableQualities: ["best"],
            formats: [],
            createdAt: now,
          },
        })),
      };
    },
    // 示例模式下：一键清空历史。
    async clearProbeRecords() {
      await delay(60);
      const arr = _loadProbe();
      _saveProbe([]);
      return { deleted: arr.length };
    },
    // 示例模式下：删除单条历史。
    async deleteProbeRecord(id) {
      await delay(40);
      const arr = _loadProbe().filter((r) => r.id !== id);
      _saveProbe(arr);
    },
    async listTasks() { await delay(300); return tasks.map((t) => ({ ...t })); },
    // 示例模式下返回常用源语言选项。
    async listVideoLanguages() { await delay(80); return ["en", "zh", "hi", "es", "ar", "fr", "pt", "ru", "de", "ko", "ja"]; },
    // 示例模式下返回目标语言选项。
    async listTargetLanguages() {
      await delay(80);
      return (
        CFG.TARGET_LANGUAGES ||
        Object.keys(LANG_LABEL).filter((k) => k !== "auto" && k !== "zh")
      ).filter((k) => k !== "bn" && k !== "ur");
    },
    // 示例模式下返回带后端标识的 Whisper 模型权重选项。
    async listModelWeights() { await delay(80); return ["local:tiny", "local:base", "local:small", "local:medium", "local:large-v3", "local:large-v3-turbo", "replicate:tiny.en", "replicate:tiny", "replicate:base.en", "replicate:base", "replicate:small.en", "replicate:small", "replicate:medium.en", "replicate:medium", "replicate:large-v1", "replicate:large-v2"]; },
    async listLocalModels() {
      await delay(40);
      let downloaded = [];
      try {
        const saved = localStorage.getItem("subtrans_mock_local_models_v1");
        downloaded = saved === null ? ["tiny"] : JSON.parse(saved);
      } catch (_) {}
      const ready = new Set(Array.isArray(downloaded) ? downloaded : ["tiny"]);
      const official = ["tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large-v1", "large-v2", "large-v3", "large-v3-turbo"];
      const sizes = ["~75 MB", "~75 MB", "~145 MB", "~145 MB", "~465 MB", "~465 MB", "~1.5 GB", "~1.5 GB", "~3 GB", "~3 GB", "~3 GB", "~1.6 GB"];
      return [
        ...official.map((name, i) => ({ name, label: `Whisper ${name}`, size: sizes[i], status: ready.has(name) ? "READY" : "NOT_INSTALLED", format: "ctranslate2", source: "official" })),
        ...[...ready].filter((name) => !official.includes(name)).map((name) => ({ name, label: name, size: null, status: "READY", format: "huggingface", source: "imported" })),
      ];
    },
    async downloadLocalModel(name) {
      await delay(40);
      let downloaded = [];
      try { downloaded = JSON.parse(localStorage.getItem("subtrans_mock_local_models_v1") || "[]"); } catch (_) {}
      if (!Array.isArray(downloaded)) downloaded = [];
      if (!downloaded.includes(name)) downloaded.push(name);
      localStorage.setItem("subtrans_mock_local_models_v1", JSON.stringify(downloaded));
      return { name, status: "READY", format: "ctranslate2", source: "official" };
    },
    async importLocalModel(name, label) {
      await delay(40);
      let downloaded = [];
      try { downloaded = JSON.parse(localStorage.getItem("subtrans_mock_local_models_v1") || "[]"); } catch (_) {}
      if (!Array.isArray(downloaded)) downloaded = [];
      if (!downloaded.includes(name)) downloaded.push(name);
      try { localStorage.setItem("subtrans_mock_local_models_v1", JSON.stringify(downloaded)); } catch (_) {}
      return { name, label: label || name, status: "READY", format: "huggingface" };
    },
    async checkLocalModel(name) {
      await delay(40);
      return { name, status: "READY", format: "huggingface" };
    },
    async deleteLocalModel(name) {
      await delay(40);
      let downloaded = [];
      try {
        const saved = localStorage.getItem("subtrans_mock_local_models_v1");
        downloaded = saved === null ? ["tiny"] : JSON.parse(saved);
      } catch (_) {}
      if (!Array.isArray(downloaded)) downloaded = [];
      const remaining = downloaded.filter((item) => item !== name);
      try { localStorage.setItem("subtrans_mock_local_models_v1", JSON.stringify(remaining)); } catch (_) {}
      return { name, label: `Whisper ${name}`, size: "", status: "NOT_INSTALLED", phase: "idle", progress: 0 };
    },
    async getAudioSettings() {
      await delay(40);
      try { return JSON.parse(localStorage.getItem("subtrans_mock_audio_settings_v1")) || { enabled: false, backend: "demucs", model: "htdemucs", threads: 1, timeout: 1800, demucsInstalled: true, ffmpegAvailable: true, ready: true, message: null }; } catch (_) { return { enabled: false, backend: "demucs", model: "htdemucs", threads: 1, timeout: 1800, demucsInstalled: true, ffmpegAvailable: true, ready: true, message: null }; }
    },
    async updateAudioSettings(payload) {
      const current = await this.getAudioSettings();
      const next = { ...current, ...payload };
      localStorage.setItem("subtrans_mock_audio_settings_v1", JSON.stringify(next));
      return next;
    },
    async getReplicateSettings() {
      await delay(40);
      try {
        return JSON.parse(localStorage.getItem("subtrans_mock_replicate_settings_v1")) || {
          hasApiToken: false,
          whisperModel: "stayallive/whisper-subtitles:b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969",
          timeout: 1800, retries: 3, retryInterval: 3600, pollInterval: 30,
        };
      } catch (_) {
        return { hasApiToken: false, whisperModel: "stayallive/whisper-subtitles:b97ba81004e7132181864c885a76cae0e56bc61caa4190a395f6d8ba45b7a969", timeout: 1800, retries: 3, retryInterval: 3600, pollInterval: 30 };
      }
    },
    async updateReplicateSettings(payload) {
      const current = await this.getReplicateSettings();
      const next = { ...current, ...payload, hasApiToken: payload.apiToken === "" ? false : (payload.apiToken ? true : current.hasApiToken) };
      delete next.apiToken;
      localStorage.setItem("subtrans_mock_replicate_settings_v1", JSON.stringify(next));
      return next;
    },
    async getReplicateBalance() {
      await delay(80);
      return {
        status: "unsupported", authenticated: false, account: null, balance: null,
        currency: "USD", balanceSupported: false, source: "mock",
        billingUrl: "https://replicate.com/account/billing", checkedAt: Date.now(),
        errorCode: "mock_mode",
        message: "示例模式不会访问真实 Replicate 账户；切换真实 API 后可检测 Token 状态。",
      };
    },
    async listTranslationEngines() {
      try { return JSON.parse(localStorage.getItem("subtrans_mock_engines_v1") || "[]"); } catch (_) { return []; }
    },
    async createTranslationEngine(payload) {
      const list = await this.listTranslationEngines();
      const item = { ...payload, id: uid().replace("task_", "engine_"), hasApiKey: !!payload.apiKey, availability: payload.apiKey ? "AVAILABLE" : "UNCONFIGURED", lastCheckedAt: Date.now() };
      list.push(item); localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); return item;
    },
    async updateTranslationEngine(id, payload) {
      const list = await this.listTranslationEngines();
      const old = list.find((e) => e.id === id) || {};
      const item = { ...old, ...payload, id, hasApiKey: !!(payload.apiKey || old.hasApiKey), availability: (payload.apiKey || old.hasApiKey) ? "AVAILABLE" : "UNCONFIGURED" };
      localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list.map((e) => e.id === id ? item : e))); return item;
    },
    async validateTranslationEngine(id) { const list = await this.listTranslationEngines(); const item = list.find((e) => e.id === id); if (item) { item.availability = item.hasApiKey ? "AVAILABLE" : "UNCONFIGURED"; localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); } return item || {}; },
    async deleteTranslationEngine(id) { const list = (await this.listTranslationEngines()).filter((e) => e.id !== id); localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); },
    async deleteTask(id) { tasks = tasks.filter((t) => t.id !== id); persist(); await delay(80); },
    async cancelTask(id) {
      const t = find(id);
      if (t) { t.status = "CANCELLED"; t.error = "用户取消"; t._sim = false; persist(); }
      return { ...t };
    },
    async retryTask(id) {
      const t = find(id);
      if (t) { t.status = "PENDING"; t.progress = 0; t.error = null; t._sim = true; persist(); }
      return { ...t };
    },
    // 示例模式下模拟打开任务文件夹。
    async openFolder() { await delay(80); },
    async folderCapability() { return { mode: "browser" }; },
    async listTaskFiles() { return { path: "", entries: [] }; },
    taskFileUrl() { return "#"; },
    // 示例模式下返回空统计 / 空预览，方便 UI 演练。
    async getStorageStats() {
      await delay(60);
      return { totalBytes: 0, totalTasks: 0, runnableTaskCount: 0, byKind: {}, byTask: [] };
    },
    async startDriveUpload(taskId, artifactNames = []) {
      await delay(60);
      return {
        batchId: `mock-drive-${taskId}`,
        taskId,
        direction: "UPLOAD",
        state: "SUCCESS",
        folderName: taskId,
        totalEntries: artifactNames.length,
        completedEntries: artifactNames.length,
        pendingArtifacts: [],
        entries: artifactNames.map((name) => ({ name, state: "SUCCESS", completedBytes: 0 })),
      };
    },
    async startDriveDownload(taskId) {
      await delay(60);
      return { batchId: `mock-drive-${taskId}`, taskId, direction: "DOWNLOAD", state: "SUCCESS", folderName: taskId, pendingArtifacts: [], entries: [] };
    },
    async getDriveBatch(batchId) { await delay(40); return { batchId, state: "SUCCESS", pendingArtifacts: [], entries: [] }; },
    async retryDriveBatch(batchId) { await delay(40); return { batchId, state: "SUCCESS", pendingArtifacts: [], entries: [] }; },
    async cancelDriveBatch(batchId) { await delay(40); return { batchId, state: "CANCELLED", pendingArtifacts: [], entries: [] }; },
    async previewCleanup() {
      await delay(60);
      return { matchedTasks: 0, matchedBytes: 0, skippedTasks: [], targets: [] };
    },
    async runCleanup(payload) {
      await delay(60);
      const probeReq = payload && payload.cleanupProbeRecordsOlderThanDays !== undefined && payload.cleanupProbeRecordsOlderThanDays !== null;
      return {
        deletedTasks: 0,
        deletedBytes: 0,
        skippedTasks: [],
        partial: [],
        deletedProbeRecords: 0,
        note: probeReq
          ? "本次清理仅作用于 probe 记录（未发现过期记录），任务产物未动"
          : "未发现符合条件的待清理任务产物",
      };
    },
    async getRetention() {
      await delay(40);
      try {
        return JSON.parse(localStorage.getItem("subtrans_mock_retention") || "null")
          || { days: 30, updatedAt: null };
      } catch (e) {
        return { days: 30, updatedAt: null };
      }
    },
    async putRetention(days) {
      await delay(40);
      const out = { days, updatedAt: Date.now() };
      try { localStorage.setItem("subtrans_mock_retention", JSON.stringify(out)); } catch (e) {}
      return out;
    },

    // 示例模式下的字幕编辑：内存中维护一份，供前端 UI 调试
    async getSubtitles(id) {
      await delay(120);
      const t = find(id);
      if (!t) throw new Error("任务不存在");
      const seed = MockApi._subtitles[id] || MockApi._seedSubs(t);
      MockApi._subtitles[id] = seed;
      return { taskId: id, title: t.title, burn: t.burn || "hard", ...seed };
    },
    async saveSubtitles(id, payload) {
      await delay(160);
      const cur = MockApi._subtitles[id] || {};
      cur[payload.locale] = payload.entries;
      if (payload.version) cur[`__v__${payload.locale}__${payload.version}`] = payload.entries;
      MockApi._subtitles[id] = cur;
      return {
        ok: true,
        taskId: id,
        locale: payload.locale,
        path: payload.version ? `${payload.locale}.${payload.version}.srt` : `${payload.locale}.srt`,
        count: payload.entries.length,
      };
    },
    async reburnSubtitles(id /* , payload */) {
      await delay(900);
      return { ok: true, taskId: id, mode: "hard", outputPath: "output_hard.mp4" };
    },
    subscribeProgress(id, onUpdate) {
      const t = find(id);
      if (!t || !t._sim) return () => {};
      let stopped = false;
      const tick = () => {
        if (stopped) return;
        t.progress = clamp(t.progress + 3 + Math.random() * 8, 0, 100);
        t.status = statusForProgress(t.progress);
        if (t.progress >= 100) {
          t.status = "SUCCESS"; t.title = "示例视频 · " + shortUrl(t.url);
          t.outputs = { video: "#", subtitle: "#" }; t._sim = false;
        }
        persist();
        onUpdate({ id: t.id, status: t.status, progress: Math.round(t.progress), title: t.title, outputs: t.outputs, error: t.error });
        if (!TERMINAL.has(t.status)) setTimeout(tick, 700 + Math.random() * 500);
      };
      setTimeout(tick, 500);
      return () => { stopped = true; };
    },
    downloadUrl() { return "#"; },
  };
})();

export const Api = USE_MOCK ? MockApi : RealApi;
