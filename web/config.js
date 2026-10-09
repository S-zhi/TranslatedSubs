// 前端运行时配置。后端就绪后，把 USE_MOCK 改为 false 即可对接真实接口。
const _localFrontendHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const _isLocalDevServer =
  _localFrontendHosts.has(window.location.hostname) && window.location.port === "5273";
const _defaultApiBaseUrl =
  window.location.protocol === "file:" || _isLocalDevServer
    ? "http://localhost:8000"
    : window.location.origin;

window.APP_CONFIG = {
  // FastAPI 后端地址（REST + SSE 同源）。云端默认跟随当前页面域名。
  API_BASE_URL: _defaultApiBaseUrl,

  // true  = 纯前端 mock，自动模拟整条流水线进度，无需后端
  // false = 走真实 REST + SSE 接口
  USE_MOCK: false,

  // 界面语言：auto 跟随浏览器；用户在页面中的选择会保存在 localStorage 并优先生效。
  UI_LOCALE: "auto",

  // 请求超时（毫秒）
  API_TIMEOUT_MS: 15000,

  // Google Drive sidecar 地址。大文件分片请求会使用更长的超时。
  DRIVE_API_BASE_URL: "http://127.0.0.1:8787",
  DRIVE_API_TIMEOUT_MS: 600000,

  // 高级设置中的引擎配置由后端动态加载；此项仅作为无后端时的兼容兜底
  TRANSLATION_ENGINES: [{
    value: "local-opus-en-zh",
    label: "本地 CPU 英译中（请先下载模型）",
    enabled: true,
    apiType: "local_ct2",
    modelStatus: "NOT_INSTALLED",
    availability: "UNAVAILABLE",
  }],

  // 默认支持的目标语言列表
  TARGET_LANGUAGES: [
    "zh-CN", "zh-TW", "en", "ja", "ko", "es", "fr", "de", "ru", "it",
    "pt", "vi", "th", "ar", "id", "hi", "nl", "pl", "tr", "sv",
    "uk", "cs", "da", "fi", "el", "he", "hu", "no", "ro", "sk",
    "af", "ca", "bg", "hr", "ms", "fa", "ta", "sw",
  ],
};

// 运行时覆盖 API 地址：localStorage.setItem('SUBTRANS_API_BASE_URL', 'http://...')
const _override = (() => {
  try {
    return localStorage.getItem("SUBTRANS_API_BASE_URL");
  } catch (e) {
    return null;
  }
})();
if (_override) window.APP_CONFIG.API_BASE_URL = _override;
