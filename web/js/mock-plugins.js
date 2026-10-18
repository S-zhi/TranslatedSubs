/* Mock counterpart of the revisioned plugin settings API. */

import { PLUGIN_STAGES, editablePluginFields, parsePluginConfig } from "./plugin-settings.js";

const STORE_KEY = "subtrans_mock_plugin_settings_v1";
const integer = (label, minimum, maximum, value) => ({ type: "integer", label, minimum, maximum, default: value });
const PLUGINS = [
  ["builtin.ytdlp", "download", "yt-dlp 下载", "下载视频并探测媒体信息", { proxy: { type: "string", label: "代理地址" }, socket_timeout: integer("连接超时（秒）", 1, 86400, 30), retries: integer("重试次数", 0, 100, 5), concurrent_fragments: integer("分片并发", 1, 32, 4) }, []],
  ["builtin.ffmpeg_audio", "audio_extract", "FFmpeg 音频提取", "为转写和配音提取音轨", {}, []],
  ["builtin.vocal_demucs", "vocal_separate", "Demucs 人声分离", "分离人声与背景音轨", { model: { type: "string", label: "分离模型" }, threads: integer("线程", 1, 8, 1), timeout_seconds: integer("超时（秒）", 60, 86400, 1800) }, []],
  ["builtin.whisper", "transcribe", "Whisper 转写", "使用已配置的 Whisper 模型转写", { backend: { type: "string", label: "转写后端", enum: ["local_whisper", "http"] }, service_url: { type: "string", label: "转写服务地址" }, timeout_seconds: integer("超时（秒）", 1, 86400, 1800), beam_size: integer("Beam size", 1, 20, 5) }, ["whisper"]],
  ["builtin.translation_engine", "translate", "翻译引擎", "使用引擎或模型卡翻译字幕", { batch_size: integer("翻译批量", 1, 200, 20), local_batch_size: integer("本地翻译批量", 1, 200, 8), timeout_seconds: integer("超时（秒）", 1, 86400, 60) }, ["engine", "llm"]],
  ["builtin.kokoro", "tts", "Kokoro 配音", "按字幕时间线合成中文或英文配音", { speed: { type: "number", label: "语速", minimum: 0.1, maximum: 4, default: 1 }, model_id: { type: "string", label: "Kokoro 模型", enum: [null, "kokoro-v1", "hexgrad/Kokoro-82M-v1.1-zh"], nullable: true } }, ["tts"]],
  ["builtin.ffmpeg_render", "render", "FFmpeg 字幕与配音封装", "生成硬字幕、软字幕及配音视频", {}, []],
  ["builtin.cobalt", "download", "Cobalt 下载", "通过自托管或已授权的 Cobalt 服务下载；API Key 在运行设置中配置；探测不提供格式列表，可显式开启 yt-dlp 回退", {
    endpoint: { type: "string", label: "Cobalt 服务地址", required: true, format: "url", maxLength: 2048 },
    socket_timeout: integer("连接超时（秒）", 1, 300, 30),
    access_mode: { type: "string", label: "访问方式", enum: ["anonymous", "api_key"], default: "anonymous" },
    fallback_policy: { type: "string", label: "失败后切换", enum: ["none", "ytdlp"], default: "none" },
    filename_style: { type: "string", label: "文件名风格", enum: ["classic", "pretty", "basic", "nerdy"], default: "basic" },
    audio_format: { type: "string", label: "音频格式", enum: ["best", "mp3", "ogg", "wav", "opus"], default: "best" },
  }, []],
].map(([plugin_id, stage, name, description, config_schema, bindings]) => ({
  plugin_id, implementation_version: "1", stage, name, description, config_schema, bindings,
  operations: stage === "render" ? ["subtitle", "dub_mux"] : [stage],
  capabilities: stage === "download" ? ["probe"] : [], config_schema_version: 1,
}));
const DEFAULT_PLUGINS = PLUGIN_STAGES.map(({ id }) => PLUGINS.find((plugin) => plugin.stage === id));

const copy = (value) => JSON.parse(JSON.stringify(value));
const fail = (message, status = 422, code = "PLUGIN_INVALID") => {
  const error = new Error(message);
  error.status = status;
  error.code = code;
  error.detail = { code, message };
  throw error;
};

export function createMockPluginApi({ storage = () => globalThis.localStorage, getModels = async () => ({}), getEngines = async () => [] } = {}) {
  let memory = null;
  function read() {
    try {
      const saved = JSON.parse(storage()?.getItem(STORE_KEY) || "null");
      if (saved && Number.isInteger(saved.revision) && Array.isArray(saved.instances)) return saved;
    } catch (_) {}
    return memory || {
      revision: 1,
      instances: DEFAULT_PLUGINS.map((plugin) => ({
        id: `default-${plugin.stage}`, name: `${plugin.name} · 默认`, stage: plugin.stage,
        plugin_id: plugin.plugin_id, implementation_version: plugin.implementation_version,
        config_schema_version: plugin.config_schema_version, enabled: true, config: {}, model_ref: null, engine_ref: null,
      })),
      defaults: Object.fromEntries(DEFAULT_PLUGINS.map((plugin) => [plugin.stage, `default-${plugin.stage}`])),
    };
  }
  function write(state) {
    memory = copy(state);
    try { storage()?.setItem(STORE_KEY, JSON.stringify(state)); } catch (_) {}
  }
  function atRevision(revision) {
    pluginRevision(revision);
    const state = read();
    if (state.revision !== revision) fail("插件配置已更新，请刷新后重试", 409, "PLUGIN_REVISION_CONFLICT");
    return state;
  }
  function pluginRevision(revision) {
    if (!Number.isInteger(revision) || revision < 0) fail("必须提供有效的 If-Match 版本", 422);
  }
  async function validate(record) {
    const plugin = PLUGINS.find((item) => item.plugin_id === record.plugin_id && item.implementation_version === record.implementation_version && item.stage === record.stage);
    if (!plugin) fail("未知插件或版本与阶段不匹配");
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(record.id || "") || !record.name?.trim()) fail("实例 ID 或名称无效");
    if (record.config_schema_version !== plugin.config_schema_version || typeof record.enabled !== "boolean") fail("配置版本或启用状态无效");
    if (record.model_ref && record.engine_ref) fail("模型与引擎绑定不能同时设置");
    if (record.model_ref?.kind === "whisper" && ["backend", "service_url"].some((key) => Object.hasOwn(record.config || {}, key))) fail("绑定 Whisper 时后端与服务地址由模型卡提供");
    if (record.model_ref) {
      const { kind, card_id } = record.model_ref;
      const models = await getModels();
      if (!plugin.bindings.includes(kind) || !models[kind]?.models?.some((card) => card.id === card_id)) fail("绑定的模型卡不存在");
    }
    if (record.engine_ref) {
      const engines = await getEngines();
      if (!plugin.bindings.includes("engine") || !engines.some((engine) => engine.id === record.engine_ref.id)) fail("绑定的翻译引擎不存在");
    }
    const fields = new Set(editablePluginFields(plugin.config_schema).map(([key]) => key));
    if (!record.config || typeof record.config !== "object" || Array.isArray(record.config) || Object.keys(record.config).some((key) => !fields.has(key))) fail("插件配置包含不支持的字段");
    const parsed = parsePluginConfig(plugin.config_schema, record.config);
    if (Object.keys(parsed.errors).length) fail(Object.values(parsed.errors).join("；"));
    if (plugin.plugin_id === "builtin.cobalt") {
      const endpoint = parsed.config.endpoint;
      try {
        if (!/^https?:\/\//.test(endpoint) || /[\s\\?#]/.test(endpoint)) throw new Error();
        const url = new URL(endpoint);
        if (!url.hostname || url.username || url.password || url.port === "0" || /\/\/[^/]*@/.test(endpoint)) throw new Error();
      } catch (_) { fail("cobalt 服务地址必须是无凭据的 HTTP(S) URL"); }
      parsed.config.endpoint = endpoint.replace(/\/+$/, "") + "/";
    }
    return { ...record, config: parsed.config, model_ref: record.model_ref || null, engine_ref: record.engine_ref || null };
  }
  return {
    async getPluginCatalog() { return { plugins: copy(PLUGINS) }; },
    async listPluginInstances() { return copy(read()); },
    async getPluginInstance(id) {
      const state = read();
      const instance = state.instances.find((item) => item.id === id);
      if (!instance) fail("插件实例不存在", 404, "PLUGIN_NOT_FOUND");
      return { revision: state.revision, instance: copy(instance) };
    },
    async createPluginInstance(payload, revision) {
      let state = atRevision(revision);
      if (state.instances.some((item) => item.id === payload.id)) fail("实例 ID 已存在", 409);
      const instance = await validate(copy(payload));
      state = atRevision(revision);
      state.instances.push(instance);
      state.revision += 1;
      write(state);
      return { revision: state.revision, instance: copy(instance) };
    },
    async updatePluginInstance(id, payload, revision) {
      let state = atRevision(revision);
      const previous = state.instances.find((item) => item.id === id);
      if (!previous) fail("插件实例不存在", 404);
      if (payload.id && payload.id !== id) fail("实例 ID 不能修改");
      const instance = await validate({ ...previous, ...copy(payload), id });
      state = atRevision(revision);
      if ((!instance.enabled || instance.stage !== previous.stage) && Object.values(state.defaults).includes(id)) fail("请先切换默认实例再停用或修改阶段", 409);
      state.instances = state.instances.map((item) => item.id === id ? instance : item);
      state.revision += 1;
      write(state);
      return { revision: state.revision, instance: copy(instance) };
    },
    async deletePluginInstance(id, revision) {
      const state = atRevision(revision);
      if (Object.values(state.defaults).includes(id)) fail("请先切换默认实例再删除", 409);
      if (!state.instances.some((item) => item.id === id)) fail("插件实例不存在", 404);
      state.instances = state.instances.filter((item) => item.id !== id);
      state.revision += 1;
      write(state);
      return { revision: state.revision, ok: true };
    },
    async updatePluginDefaults(defaults, revision) {
      const state = atRevision(revision);
      for (const [stage, id] of Object.entries(defaults)) {
        if (!PLUGIN_STAGES.some((item) => item.id === stage) || !state.instances.some((item) => item.id === id && item.stage === stage && item.enabled)) fail("默认实例必须为当前阶段已启用实例");
      }
      state.defaults = { ...state.defaults, ...defaults };
      state.revision += 1;
      write(state);
      return { revision: state.revision, defaults: copy(state.defaults) };
    },
  };
}
