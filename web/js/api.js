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
  const { requestTimeoutMs, ...fetchOptions } = options;
  const timeoutMs = Number(requestTimeoutMs || CFG.API_TIMEOUT_MS) || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${base}${path}`, {
      ...fetchOptions,
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
  let detailValue = null;
  try {
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      if (data && data.detail) {
        detailValue = data.detail;
        if (Array.isArray(data.detail)) {
          detail = data.detail.map((item) => `${item.loc?.join(".")}: ${item.msg}`).join("; ");
        } else if (typeof data.detail === "object") {
          const message = data.detail.message || data.detail.code || "请求失败";
          const suggestion = data.detail.suggestion;
          const knownSuggestion = {
            MODEL_NOT_READY: "请先下载或导入并检查模型，再到模型设置中启用。",
            MODEL_CURRENT: "请先在模型设置中切换当前模型。",
            MODEL_IN_USE: "请等待使用该模型的任务结束后重试。",
          }[data.detail.code];
          detail = [message, suggestion || knownSuggestion].filter(Boolean).join("；");
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
  const error = new Error(`${fallback}：${detail}`);
  error.status = res.status;
  error.detail = detailValue;
  error.code = detailValue && typeof detailValue === "object" ? detailValue.code : undefined;
  return error;
}

const MODEL_SETTING_FIELDS = {
  llm: new Set(["enabled", "provider", "baseUrl", "model", "apiKey", "temperature", "maxTokens", "timeout", "concurrency", "voice", "models"]),
  whisper: new Set(["enabled", "provider", "model", "language", "device", "threads", "wordTimestamps", "models"]),
  tts: new Set(["enabled", "provider", "baseUrl", "model", "apiKey", "voice", "speed", "format", "models"]),
};
const MODEL_CARD_FIELDS = new Set(["id", "name", "enabled", "provider", "baseUrl", "model", "apiKey", "temperature", "maxTokens", "timeout", "concurrency", "language", "device", "threads", "wordTimestamps", "voice", "speed", "format", "isCurrent"]);
const MOCK_WHISPER_ASSETS_KEY = "subtrans_mock_whisper_assets_v2";

function modelApiError(code, message, status = 409) {
  const error = new Error(message);
  error.code = code;
  error.status = status;
  error.detail = { code, message };
  return error;
}

const OFFICIAL_WHISPER_MODELS = [
  ["tiny", "~75 MB"], ["tiny.en", "~75 MB"], ["base", "~145 MB"], ["base.en", "~145 MB"],
  ["small", "~465 MB"], ["small.en", "~465 MB"], ["medium", "~1.5 GB"], ["medium.en", "~1.5 GB"],
  ["large-v1", "~3 GB"], ["large-v2", "~3 GB"], ["large-v3", "~3 GB"], ["large-v3-turbo", "~1.6 GB"],
];

async function validateImportedWhisperFiles(files = []) {
  const list = Array.from(files || []);
  const names = new Set();
  const allowed = /^(config\.json|generation_config\.json|preprocessor_config\.json|tokenizer_config\.json|tokenizer\.json|vocab\.json|merges\.txt|normalizer\.json|special_tokens_map\.json|added_tokens\.json|pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors|pytorch_model\.bin\.index\.json|model\.safetensors\.index\.json)$/;
  for (const file of list) {
    const name = String(file?.name || "");
    if (!name || name !== name.split(/[\\/]/).pop() || name.startsWith(".") || !allowed.test(name)) {
      throw modelApiError("MODEL_INVALID", `不支持的模型文件: ${name || "(空文件名)"}`, 422);
    }
    if (names.has(name)) throw modelApiError("MODEL_INVALID", `模型文件重复: ${name}`, 422);
    if (Number(file.size) === 0) throw modelApiError("MODEL_INVALID", `模型文件为空: ${name}`, 422);
    names.add(name);
  }
  const weights = [...names].filter((name) => /^(pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors)$/.test(name));
  if (!names.has("config.json") || !names.has("preprocessor_config.json")
    || !(names.has("tokenizer.json") || (names.has("vocab.json") && names.has("merges.txt")))
    || !weights.length) {
    throw modelApiError("MODEL_INVALID", "缺少有效的 config.json、preprocessor_config.json、Whisper tokenizer 文件或 .bin/.safetensors 权重", 422);
  }
  const suffixes = new Set(weights.map((name) => name.endsWith(".bin") ? "bin" : "safetensors"));
  if (suffixes.size > 1) throw modelApiError("MODEL_INVALID", "不能混用 .bin 和 .safetensors 权重", 422);
  const weightIndex = [...names].find((name) => /^(pytorch_model\.bin|model\.safetensors)\.index\.json$/.test(name));
  const sharded = weights.length > 1 || weights.some((name) => name.includes("-of-"));
  if (sharded && !weightIndex) throw modelApiError("MODEL_INVALID", "分片权重缺少索引文件", 422);
  if (!sharded && weightIndex) throw modelApiError("MODEL_INVALID", "单文件权重不应带分片索引", 422);
  const configFile = list.find((file) => file.name === "config.json");
  if (typeof configFile?.text === "function") {
    let config;
    try { config = JSON.parse(await configFile.text()); } catch (_) { throw modelApiError("MODEL_INVALID", "缺少有效的 config.json", 422); }
    if (config?.model_type !== "whisper") throw modelApiError("MODEL_INVALID", "只支持 Hugging Face Whisper 语音识别模型", 422);
  }
  if (weightIndex) {
    const indexFile = list.find((file) => file.name === weightIndex);
    if (typeof indexFile?.text === "function") {
      let declared;
      try { declared = new Set(Object.values(JSON.parse(await indexFile.text()).weight_map)); } catch (_) { throw modelApiError("MODEL_INVALID", "权重索引格式无效", 422); }
      if (declared.size !== weights.length || weights.some((name) => !declared.has(name))) {
        throw modelApiError("MODEL_INVALID", "权重分片与索引不一致", 422);
      }
    }
  }
  return [...names];
}

function filterModelSettingsPayload(payload = {}) {
  const output = {};
  for (const kind of Object.keys(MODEL_SETTING_FIELDS)) {
    const config = payload?.[kind];
    if (!config || typeof config !== "object" || Array.isArray(config)) continue;
    const allowed = MODEL_SETTING_FIELDS[kind];
    const clean = Object.fromEntries(Object.entries(config).filter(([key]) => allowed.has(key)));
    if (Array.isArray(clean.models)) {
      clean.models = clean.models.map((model) => Object.fromEntries(
        Object.entries(model || {}).filter(([key]) => MODEL_CARD_FIELDS.has(key)),
      ));
    }
    output[kind] = clean;
  }
  return output;
}

const RealApi = {
  base: CFG.API_BASE_URL,

  async createTask(payload) {
    const res = await request(this.base, "/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "创建任务失败");
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
    if (!res.ok) throw await readError(res, "上传任务创建失败");
    return res.json();
  },

  // 探测链接是否能被 yt-dlp 解析并找到可下载格式。
  async probeVideo(url, options = {}) {
    const res = await request(this.base, "/api/tasks/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(probeRequestBody(url, options)),
    });
    if (!res.ok) throw await readError(res, "链接校验失败");
    return res.json();
  },

  // 获取 yt-dlp 运行时版本与提取器环境信息。
  async getYtDlpInfo() {
    const res = await request(this.base, "/api/tasks/probe/ytdlp-info");
    if (!res.ok) throw await readError(res, "获取 yt-dlp 环境信息失败");
    return res.json();
  },

  // 列出最近的下载测试历史记录（按时间倒序）。
  async listProbeRecords(limit = 50) {
    const res = await request(
      this.base,
      `/api/tasks/probe/records?limit=${encodeURIComponent(limit)}`,
    );
    if (!res.ok) throw await readError(res, "获取测试历史失败");
    return res.json();
  },

  // 获取固定十站点启动探测与开发者页重测的实时状态。
  async getProbeStartupStatus() {
    const res = await request(this.base, "/api/tasks/probe/startup-status");
    if (!res.ok) throw await readError(res, "获取站点可用性状态失败");
    return res.json();
  },

  // 一键清空所有下载测试历史。
  async clearProbeRecords() {
    const res = await request(this.base, "/api/tasks/probe/records", {
      method: "DELETE",
    });
    if (!res.ok) throw await readError(res, "清空测试历史失败");
    return res.json();
  },

  // 删除单条下载测试历史。
  async deleteProbeRecord(id) {
    const res = await request(this.base, `/api/tasks/probe/records/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw await readError(res, "删除测试记录失败");
  },

  async listTasks() {
    const res = await request(this.base, "/api/tasks");
    if (!res.ok) throw await readError(res, "获取任务列表失败");
    return res.json();
  },

  // 获取源视频语言选项。
  async listVideoLanguages() {
    const res = await request(this.base, "/api/srt/languages");
    if (!res.ok) throw await readError(res, "获取源语言失败");
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
    if (!res.ok) throw await readError(res, "获取模型列表失败");
    return res.json();
  },

  async listLocalModels() {
    const res = await request(this.base, "/api/srt/local-models");
    if (!res.ok) throw await readError(res, "获取本地模型失败");
    return res.json();
  },

  async downloadLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}/download`, { method: "POST" });
    if (!res.ok) throw await readError(res, "下载官方模型失败");
    return res.json();
  },

  async importLocalModel(name, label, files) {
    const body = new FormData();
    body.append("name", name);
    body.append("label", label);
    for (const file of files) body.append("files", file, file.name);
    const res = await request(this.base, "/api/srt/local-models/import", {
      method: "POST", body, requestTimeoutMs: 10 * 60 * 1000,
    });
    if (!res.ok) throw await readError(res, "导入本地模型失败");
    return res.json();
  },

  async checkLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}/check`, { method: "POST" });
    if (!res.ok) throw await readError(res, "检查本地模型失败");
    return res.json();
  },

  async deleteLocalModel(name) {
    const res = await request(this.base, `/api/srt/local-models/${encodeURIComponent(name)}`, { method: "DELETE" });
    if (!res.ok) throw await readError(res, "删除本地模型失败");
    return res.json();
  },

  async listTranslationEngines() {
    const res = await request(this.base, "/api/settings/translation-engines");
    if (!res.ok) throw await readError(res, "获取翻译引擎失败");
    return res.json();
  },

  async getAudioSettings() {
    const res = await request(this.base, "/api/settings/audio");
    if (!res.ok) throw await readError(res, "读取音频设置失败");
    return res.json();
  },

  async updateAudioSettings(payload) {
    const res = await request(this.base, "/api/settings/audio", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "保存音频设置失败");
    return res.json();
  },

  async createTranslationEngine(payload) {
    const res = await request(this.base, "/api/settings/translation-engines", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "创建翻译引擎失败");
    return res.json();
  },

  async updateTranslationEngine(id, payload) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "保存翻译引擎失败");
    return res.json();
  },

  async validateTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}/validate`, {
      method: "POST",
    });
    if (!res.ok) throw await readError(res, "检测翻译引擎失败");
    return res.json();
  },

  async downloadTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}/download`, {
      method: "POST",
    });
    if (!res.ok) throw await readError(res, "下载本地翻译模型失败");
    return res.json();
  },

  async deleteTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) throw await readError(res, "删除翻译引擎失败");
  },

  // 统一模型设置：后端返回的 API Key 应为脱敏值或仅 hasApiKey 标志。
  async getModelSettings() {
    const res = await request(this.base, "/api/settings/models");
    if (!res.ok) throw await readError(res, "读取模型设置失败");
    return res.json();
  },

  async getRuntimeReadiness() {
    const res = await request(this.base, "/api/health/ready");
    if (!res.ok) throw await readError(res, "读取运行时模型状态失败");
    return res.json();
  },

  async updateModelSettings(payload) {
    const res = await request(this.base, "/api/settings/models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(filterModelSettingsPayload(payload)),
    });
    if (!res.ok) throw await readError(res, "保存模型设置失败");
    return res.json();
  },

  async createModelSettings(kind, payload) {
    const res = await request(this.base, `/api/settings/models/${encodeURIComponent(kind)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "新增模型失败");
    return res.json();
  },

  async validateModelSettings(kind, payload) {
    const res = await request(this.base, `/api/settings/models/${encodeURIComponent(kind)}/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "模型连接检查失败");
    return res.json();
  },

  async deleteTask(id) {
    const res = await request(this.base, `/api/tasks/${id}`, { method: "DELETE" });
    if (!res.ok) throw await readError(res, "删除失败");
  },

  async cancelTask(id) {
    const res = await request(this.base, `/api/tasks/${id}/cancel`, { method: "POST" });
    if (!res.ok) throw await readError(res, "取消失败");
    return res.json();
  },

  async retryTask(id) {
    const res = await request(this.base, `/api/tasks/${id}/retry`, { method: "POST" });
    if (!res.ok) throw await readError(res, "重试失败");
    return res.json();
  },

  // 请求后端打开任务所在的本地文件夹。
  async openFolder(id) {
    const res = await request(this.base, `/api/tasks/${id}/folder`, { method: "POST" });
    if (!res.ok) throw await readError(res, "打开文件夹失败");
  },

  async folderCapability(id) {
    const res = await request(this.base, `/api/tasks/${encodeURIComponent(id)}/folder-capability`);
    if (!res.ok) throw await readError(res, "查询文件夹能力失败");
    return res.json();
  },

  async listTaskFiles(id, path = "") {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const res = await request(this.base, `/api/tasks/${encodeURIComponent(id)}/files${query}`);
    if (!res.ok) throw await readError(res, "读取任务文件失败");
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
    if (!res.ok) throw await readError(res, "创建 Drive 上传批次失败");
    return res.json();
  },

  async startDriveDownload(taskId) {
    const res = await request(this.base, `/api/storage/tasks/${encodeURIComponent(taskId)}/drive/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw await readError(res, "创建 Drive 下载批次失败");
    return res.json();
  },

  async getDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}`);
    if (!res.ok) throw await readError(res, "读取 Drive 批次失败");
    return res.json();
  },

  async retryDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}/retry`, { method: "POST" });
    if (!res.ok) throw await readError(res, "重试 Drive 批次失败");
    return res.json();
  },

  async cancelDriveBatch(batchId) {
    const res = await request(this.base, `/api/storage/drive/batches/${encodeURIComponent(batchId)}/cancel`, { method: "POST" });
    if (!res.ok) throw await readError(res, "取消 Drive 批次失败");
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
    if (!res.ok) throw await readError(res, "预览清理失败");
    return res.json();
  },

  async runCleanup(payload) {
    const res = await request(this.base, "/api/storage/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw await readError(res, "执行清理失败");
    return res.json();
  },

  async getRetention() {
    const res = await request(this.base, "/api/storage/retention");
    if (!res.ok) throw await readError(res, "获取保留策略失败");
    return res.json();
  },

  async putRetention(days) {
    const res = await request(this.base, "/api/storage/retention", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days }),
    });
    if (!res.ok) throw await readError(res, "保存保留策略失败");
    return res.json();
  },

  // 拉取任务 current subtitles（original + translated），解析为前端可编辑结构
  async getSubtitles(id) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`);
    if (!res.ok) throw await readError(res, "读取字幕失败");
    return res.json();
  },

  // 保存编辑后的字幕到后端；version 为可选版本号（例 "v2"）
  async saveSubtitles(id, payload) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "保存字幕失败");
    return res.json();
  },

  // 基于当前 translated.srt 重新烧录成品；mode 可选覆盖任务设置
  async reburnSubtitles(id, payload = {}) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles/burn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await readError(res, "重新烧录失败");
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

  function _dispatch(name, detail) {
    if (typeof document !== "undefined" && typeof CustomEvent !== "undefined") {
      document.dispatchEvent(new CustomEvent(name, { detail }));
    }
  }

  function _loadWhisperAssets() {
    try {
      const saved = localStorage.getItem(MOCK_WHISPER_ASSETS_KEY);
      if (saved !== null) {
        const items = JSON.parse(saved);
        if (Array.isArray(items)) return items;
      }
    } catch (_) {}
    const catalog = OFFICIAL_WHISPER_MODELS.map(([name, size]) => ({
      id: `whisper-local-${name}`, name, label: `Whisper ${name}`, size,
      status: name === "tiny" ? "READY" : "NOT_INSTALLED", format: "ctranslate2",
      source: "official", phase: name === "tiny" ? "ready" : "idle", progress: name === "tiny" ? 100 : 0,
    }));
    // Upgrade the former array-only store while preserving every installed custom/official name.
    try {
      const legacyRaw = localStorage.getItem("subtrans_mock_local_models_v1");
      const legacy = legacyRaw === null ? ["tiny"] : JSON.parse(legacyRaw);
      if (Array.isArray(legacy)) {
        for (const name of legacy) {
          const item = catalog.find((asset) => asset.name === name);
          if (item) Object.assign(item, { status: "READY", phase: "ready", progress: 100 });
          else catalog.push({ id: `whisper-imported-${name}`, name, label: name, size: null, status: "READY", format: "huggingface", source: "imported", phase: "ready", progress: 100, files: [] });
        }
      }
    } catch (_) {}
    _saveWhisperAssets(catalog);
    try { localStorage.removeItem("subtrans_mock_local_models_v1"); } catch (_) {}
    return catalog;
  }

  function _saveWhisperAssets(items) {
    try { localStorage.setItem(MOCK_WHISPER_ASSETS_KEY, JSON.stringify(items)); } catch (_) {}
  }

  function _whisperAssets() {
    return _loadWhisperAssets();
  }

  function _asset(name) {
    return _whisperAssets().find((item) => item.name === name);
  }

  function _emitWhisperAssets() {
    const items = _whisperAssets();
    const settings = _readMockSettings();
    _dispatch("whisper-models-change", { items, whisper: _settingsWithAssetStatus(settings.whisper) });
    return items;
  }

  function _effectiveTaskModel(payload) {
    const whisper = _readMockSettings().whisper;
    return whisper.runtimeManaged && whisper.enabled !== false ? whisper.model : payload.model;
  }

  function _ensureTaskModelReady(model, needSubtitle = true) {
    if (!needSubtitle) return;
    const name = String(model || "").replace(/^local:/, "");
    if (_asset(name)?.status !== "READY") {
      throw modelApiError("MODEL_NOT_READY", `本地 Whisper 模型尚未就绪: ${name}`, 409);
    }
  }

  function _readMockSettings() {
    const defaults = {
      llm: { enabled: true, provider: "deepseek", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", hasApiKey: false, temperature: 0.3, maxTokens: 4096, timeout: 120, concurrency: 2 },
      whisper: { enabled: true, provider: "local", model: "local:tiny", language: "auto", device: "cpu", threads: 4, wordTimestamps: true, runtimeManaged: false },
      tts: { enabled: false, provider: "kokoro", baseUrl: "", model: "kokoro-v1", hasApiKey: false, voice: "auto", speed: 1, format: "wav" },
    };
    try {
      const saved = JSON.parse(localStorage.getItem("subtrans_mock_model_settings_v1") || "null");
      if (!saved) return defaults;
      return { ...defaults, ...saved, llm: { ...defaults.llm, ...saved.llm }, whisper: { ...defaults.whisper, ...saved.whisper }, tts: { ...defaults.tts, ...saved.tts } };
    } catch (_) { return defaults; }
  }

  function _settingsWithAssetStatus(config) {
    const assets = _whisperAssets();
    const selectedName = String(config.model || "local:tiny").replace(/^local:/, "");
    const selected = assets.find((item) => item.name === selectedName);
    const savedCards = Array.isArray(config.models) ? config.models : [];
    const models = assets.map((item) => ({
      ...(savedCards.find((card) => String(card.model || "").replace(/^local:/, "") === item.name) || {}),
      id: savedCards.find((card) => String(card.model || "").replace(/^local:/, "") === item.name)?.id || `whisper-card-${encodeURIComponent(item.name)}`,
      name: savedCards.find((card) => String(card.model || "").replace(/^local:/, "") === item.name)?.name || item.label || item.name,
      provider: "local", model: `local:${item.name}`,
      enabled: item.status === "READY", status: item.status, isCurrent: item.name === selectedName,
    }));
    return { ...config, provider: "local", model: `local:${selectedName}`, status: selected?.status || "MISSING", models };
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
        error: "本地 Whisper 模型尚未就绪，请先下载模型",
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
      const model = _effectiveTaskModel(payload);
      _ensureTaskModelReady(model, payload.needSubtitle !== false);
      const t = {
        id: uid(), url: payload.url, title: null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model, engine: payload.engine,
        runtimeConfig: { whisper: { ..._readMockSettings().whisper, model } },
        status: "PENDING", progress: 0, error: null, createdAt: Date.now(), outputs: null, _sim: true,
      };
      tasks.unshift(t); persist(); await delay(150); return { ...t };
    },
    // 示例模式下模拟本地视频上传任务。
    async createUploadTask(payload) {
      const model = _effectiveTaskModel(payload);
      _ensureTaskModelReady(model, payload.needSubtitle !== false);
      const t = {
        id: uid(), url: payload.file?.name || "uploaded-video", title: payload.file?.name || null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model, engine: payload.engine,
        runtimeConfig: { whisper: { ..._readMockSettings().whisper, model } },
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
    async listModelWeights() {
      await delay(40);
      return _whisperAssets().filter((item) => item.status === "READY").map((item) => `local:${item.name}`);
    },
    async listLocalModels() { await delay(30); return _whisperAssets().map((item) => ({ ...item })); },
    async downloadLocalModel(name) {
      await delay(25);
      const item = _asset(name);
      if (!item || item.source !== "official") throw modelApiError("MODEL_INVALID", `未知官方模型: ${name}`, 404);
      if (item.status === "READY" || item.status === "DOWNLOADING") return { ...item };
      const assets = _whisperAssets();
      const downloading = assets.map((asset) => asset.name === name ? { ...asset, status: "DOWNLOADING", phase: "downloading", progress: 1, error: null } : asset);
      _saveWhisperAssets(downloading);
      _emitWhisperAssets();
      setTimeout(() => {
        let failNext = false;
        try { failNext = localStorage.getItem("subtrans_mock_whisper_fail_next_download") === "1"; } catch (_) {}
        try { localStorage.removeItem("subtrans_mock_whisper_fail_next_download"); } catch (_) {}
        const current = _whisperAssets();
        _saveWhisperAssets(current.map((asset) => asset.name === name
          ? { ...asset, status: failNext ? "ERROR" : "READY", phase: failNext ? "error" : "ready", progress: failNext ? 0 : 100, error: failNext ? "示例下载失败，可重试" : null }
          : asset));
        _emitWhisperAssets();
      }, 450);
      return { ...downloading.find((asset) => asset.name === name) };
    },
    async importLocalModel(name, label, files) {
      const safeName = String(name || "");
      if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(safeName) || safeName.startsWith(".")
        || OFFICIAL_WHISPER_MODELS.some(([official]) => official === safeName)) {
        throw modelApiError("MODEL_INVALID", "模型名称无效或与内置模型重名", 422);
      }
      const fileNames = await validateImportedWhisperFiles(files);
      if (["false", "0"].includes(localStorage.getItem("subtrans_mock_hf_dependency_enabled"))) {
        throw modelApiError("MODEL_DEPENDENCY_MISSING", "请先启用 Hugging Face 模型依赖：uv sync --extra hf-models", 503);
      }
      if (_asset(safeName)) throw modelApiError("MODEL_EXISTS", "模型已存在", 409);
      const asset = { id: `whisper-imported-${safeName}`, name: safeName, label: label || safeName, size: null, status: "READY", format: "huggingface", source: "imported", phase: "ready", progress: 100, files: fileNames };
      const items = [..._whisperAssets(), asset];
      _saveWhisperAssets(items);
      _emitWhisperAssets();
      await delay(25);
      return { ...asset };
    },
    async checkLocalModel(name) {
      await delay(30);
      const assets = _whisperAssets();
      const item = assets.find((asset) => asset.name === name);
      if (!item) throw modelApiError("MODEL_NOT_FOUND", `模型不存在: ${name}`, 404);
      const files = new Set(item.files || []);
      const validImported = item.source !== "imported"
        || (files.has("config.json") && files.has("preprocessor_config.json")
          && (files.has("tokenizer.json") || files.has("vocab.json") && files.has("merges.txt"))
          && [...files].some((file) => /\.(bin|safetensors)$/.test(file)));
      if (!validImported) {
        const updated = assets.map((asset) => asset.name === name ? { ...asset, status: "ERROR", phase: "error", error: "Whisper 文件结构无效" } : asset);
        _saveWhisperAssets(updated);
        _emitWhisperAssets();
        throw modelApiError("MODEL_INVALID", "缺少有效的 config.json、preprocessor_config.json、tokenizer 或 .bin/.safetensors 权重", 422);
      }
      const updated = assets.map((asset) => asset.name === name ? { ...asset, status: "READY", phase: "ready", progress: 100, error: null } : asset);
      _saveWhisperAssets(updated);
      _emitWhisperAssets();
      return { ...updated.find((asset) => asset.name === name) };
    },
    async deleteLocalModel(name) {
      await delay(25);
      const settings = _readMockSettings().whisper;
      if (String(settings.model || "").replace(/^local:/, "") === name) {
        throw modelApiError("MODEL_CURRENT", "当前正在使用这个模型，请先切换当前模型。", 409);
      }
      const inUse = tasks.some((task) => !["SUCCESS", "FAILED", "CANCELLED"].includes(task.status)
        && task.needSubtitle !== false && String(task.model || "").replace(/^local:/, "") === name);
      if (inUse) throw modelApiError("MODEL_IN_USE", "有未结束的任务选用了这个模型，暂时无法清理", 409);
      const assets = _whisperAssets();
      const target = assets.find((asset) => asset.name === name);
      const remaining = assets.flatMap((asset) => {
        if (asset.name !== name) return [asset];
        if (asset.source === "official") return [{ ...asset, status: "NOT_INSTALLED", phase: "idle", progress: 0, installedBytes: 0, error: null }];
        return [];
      });
      _saveWhisperAssets(remaining);
      _emitWhisperAssets();
      return { name, label: target?.label || `Whisper ${name}`, size: target?.size || "", status: "NOT_INSTALLED", phase: "idle", progress: 0 };
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
    async listTranslationEngines() {
      const fallbackLocal = { id: "local-opus-en-zh", name: "本地 CPU 英译中", apiType: "local_ct2", model: "Helsinki-NLP/opus-mt-en-zh", baseUrl: "", enabled: true, hasApiKey: false, availability: "UNAVAILABLE", modelStatus: "NOT_INSTALLED", installedBytes: 0 };
      try {
        const list = JSON.parse(localStorage.getItem("subtrans_mock_engines_v1") || "[]");
        return list.some((engine) => engine.id === fallbackLocal.id) ? list : [...list, fallbackLocal];
      } catch (_) { return [fallbackLocal]; }
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
    async downloadTranslationEngine(id) {
      if (id !== "local-opus-en-zh") throw new Error("未知本地翻译模型");
      const list = await this.listTranslationEngines();
      const defaults = { id, name: "本地 CPU 英译中", apiType: "local_ct2", model: "Helsinki-NLP/opus-mt-en-zh", baseUrl: "", enabled: true, hasApiKey: false, availability: "AVAILABLE", modelStatus: "READY", installedBytes: 0 };
      const next = list.some((engine) => engine.id === id)
        ? list.map((engine) => engine.id === id ? { ...engine, ...defaults } : engine)
        : [...list, defaults];
      localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(next));
      await delay(120);
      return defaults;
    },
    async deleteTranslationEngine(id) { const list = (await this.listTranslationEngines()).filter((e) => e.id !== id); localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); },
    async getModelSettings() {
      await delay(50);
      const settings = _readMockSettings();
      settings.whisper = _settingsWithAssetStatus(settings.whisper);
      return settings;
    },
    async getRuntimeReadiness() {
      await delay(25);
      const settings = _readMockSettings();
      return { ok: true, modelSettings: { whisper: _settingsWithAssetStatus(settings.whisper) } };
    },
    async updateModelSettings(payload) {
      await delay(90);
      const current = _readMockSettings();
      const cleanPayload = filterModelSettingsPayload(payload);
      const incomingWhisper = cleanPayload.whisper;
      if (incomingWhisper) {
        if ((incomingWhisper.provider || current.whisper.provider) !== "local") {
          throw modelApiError("WHISPER_PROVIDER_UNSUPPORTED", "示例模式暂不支持 Replicate 或自定义 Whisper 服务。", 422);
        }
        const requestedCards = Array.isArray(incomingWhisper.models) ? incomingWhisper.models : [];
        const chosen = requestedCards.find((model) => model.isCurrent);
        if (chosen && (chosen.provider || "local") !== "local") {
          throw modelApiError("WHISPER_PROVIDER_UNSUPPORTED", "示例模式暂不支持 Replicate 或自定义 Whisper 服务。", 422);
        }
        const selectedModel = incomingWhisper.model || chosen?.model || current.whisper.model;
        const selectedName = String(selectedModel || "").replace(/^local:/, "");
        if (!OFFICIAL_WHISPER_MODELS.some(([name]) => name === selectedName)
          && !_asset(selectedName)) throw modelApiError("MODEL_NOT_READY", `模型不存在: ${selectedName}`, 409);
        if (_asset(selectedName)?.status !== "READY") throw modelApiError("MODEL_NOT_READY", `本地 Whisper 模型尚未就绪: ${selectedName}`, 409);
        const assetCards = _whisperAssets().map((asset) => ({
          id: `whisper-card-${encodeURIComponent(asset.name)}`, name: asset.label || asset.name,
          provider: "local", model: `local:${asset.name}`,
          enabled: asset.status === "READY", status: asset.status, isCurrent: asset.name === selectedName,
        }));
        // Keep settings card IDs stable and merge incoming card edits by ID or model.
        const oldCards = Array.isArray(current.whisper.models) ? current.whisper.models : [];
        const newCards = requestedCards.reduce((map, card) => map.set(card.id, card), new Map());
        const mergedCards = assetCards.map((card) => {
          const previous = oldCards.find((item) => item.id === card.id
            || String(item.model || "").replace(/^local:/, "") === card.model.slice(6)) || {};
          const update = newCards.get(previous.id || card.id)
            || requestedCards.find((item) => String(item.model || "").replace(/^local:/, "") === card.model.slice(6)) || {};
          return {
            ...card, ...previous, ...update,
            id: previous.id || update.id || card.id,
            name: update.name || previous.name || card.name,
            provider: "local", model: card.model, enabled: card.enabled, status: card.status,
            isCurrent: card.model === `local:${selectedName}`,
          };
        });
        current.whisper = {
          ...current.whisper, ...incomingWhisper, provider: "local", model: `local:${selectedName}`,
          runtimeManaged: true, models: mergedCards,
        };
      }
      const next = {
        ...current,
        ...cleanPayload,
        llm: { ...current.llm, ...(cleanPayload.llm || {}) },
        whisper: { ...current.whisper, ...(cleanPayload.whisper || {}), ...(incomingWhisper ? current.whisper : {}) },
        tts: { ...current.tts, ...(cleanPayload.tts || {}) },
      };
      for (const kind of ["llm", "tts"]) {
        const key = next[kind].apiKey;
        if (key === "") next[kind].hasApiKey = false;
        else if (key) next[kind].hasApiKey = true;
        delete next[kind].apiKey;
      }
      localStorage.setItem("subtrans_mock_model_settings_v1", JSON.stringify(next));
      const response = { ...next, whisper: _settingsWithAssetStatus(next.whisper) };
      if (incomingWhisper) _dispatch("model-settings-change", { settings: response });
      return response;
    },
    async createModelSettings(kind, payload) {
      await delay(90);
      if (!["llm", "whisper", "tts"].includes(kind)) throw new Error("未知模型类型");
      if (kind === "whisper") throw modelApiError("WHISPER_PROVIDER_UNSUPPORTED", "Whisper 只能通过模型商店下载官方模型或导入本地文件；Replicate 和自定义服务暂不支持。", 422);
      const current = await this.getModelSettings();
      const model = {
        ...payload,
        id: `${kind}-${uid().replace("task_", "model_")}`,
        name: payload.name || payload.model || `${kind.toUpperCase()} 模型`,
        hasApiKey: Boolean(payload.apiKey),
        createdAt: Date.now(),
        isCurrent: true,
      };
      delete model.apiKey;
      const existingModels = current[kind].models || [{ ...current[kind], id: `${kind}-default`, name: current[kind].model || `${kind.toUpperCase()} 默认模型`, isCurrent: false }];
      const models = [...existingModels, model];
      const next = { ...current, [kind]: { ...current[kind], ...model, models: models.map((item) => ({ ...item, isCurrent: item.id === model.id })) } };
      localStorage.setItem("subtrans_mock_model_settings_v1", JSON.stringify(next));
      return { model, config: next[kind] };
    },
    async validateModelSettings(kind, payload) {
      await delay(120);
      if (kind === "whisper") return { ok: false, message: payload.provider === "local" ? "请先在模型商店检查本地 Whisper 模型。" : "示例模式暂不支持 Replicate 或自定义 Whisper 服务。" };
      if (!payload.model) return { ok: false, message: "请先填写模型名称" };
      return { ok: true, message: "连接成功（示例模式）", latencyMs: 86 };
    },
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
