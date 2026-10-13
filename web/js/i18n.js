/* Browser UI localization. The bundled catalog contains interface copy only. */

import { LANG_LABEL } from "./constants.js";
import { LOCALIZED_TEXT, LOCALIZED_TEMPLATES } from "./locales.js";

export const UI_LOCALES = Object.freeze({
  "zh-CN": "简体中文",
  en: "English",
  hi: "हिन्दी",
  es: "Español",
  ar: "العربية",
  fr: "Français",
  pt: "Português",
  ru: "Русский",
});

const STORAGE_KEY = "subtrans_ui_locale";
const ATTRIBUTES = ["aria-label", "placeholder", "title", "alt", "content"];
const LANGUAGE_CODES = new Map(Object.entries(LANG_LABEL).map(([code, name]) => [name, code]));
const templatePatterns = new Map();
const sortedTemplates = Object.fromEntries(Object.entries(LOCALIZED_TEMPLATES).map(([code, messages]) => [
  code,
  Object.entries(messages).sort(([a], [b]) =>
    b.replace(/⟦\d+⟧/g, "").length - a.replace(/⟦\d+⟧/g, "").length),
]));
const originals = new WeakMap();
const lastRendered = new WeakMap();
const attributeOriginals = new WeakMap();
const attributeRendered = new WeakMap();
let locale = "zh-CN";
let observer;

export function normalizeLocale(value) {
  if (typeof value !== "string") return null;
  const match = value.trim().toLowerCase().replaceAll("_", "-");
  if (match === "zh" || match.startsWith("zh-")) return "zh-CN";
  return Object.keys(UI_LOCALES).find((code) => code.toLowerCase() === match)
    || Object.keys(UI_LOCALES).find((code) => code.toLowerCase().split("-")[0] === match.split("-")[0])
    || null;
}

function readPreference() {
  try { return localStorage.getItem(STORAGE_KEY); } catch (_) { return null; }
}

function savePreference(value) {
  try { localStorage.setItem(STORAGE_KEY, value); } catch (_) { /* private mode */ }
}

export function preferredLocale({ saved, configured, languages = [] } = {}) {
  return normalizeLocale(saved)
    || normalizeLocale(configured)
    || languages.map(normalizeLocale).find(Boolean)
    || "zh-CN";
}

export function getLocale() { return locale; }

const MANUAL_TEXT = {
  "界面语言": {
    en: "Interface language", hi: "इंटरफ़ेस भाषा", es: "Idioma de la interfaz",
    ar: "لغة الواجهة", fr: "Langue de l’interface",
    pt: "Idioma da interface", ru: "Язык интерфейса",
  },
  "硬烧录": {
    en: "Burned-in subtitles", hi: "वीडियो में स्थायी उपशीर्षक", es: "Subtítulos incrustados",
    ar: "ترجمة مدمجة", fr: "Sous-titres incrustés",
    pt: "Legendas embutidas", ru: "Вшитые субтитры",
  },
  "软字幕": {
    en: "Switchable subtitles", hi: "बदले जा सकने वाले उपशीर्षक", es: "Subtítulos opcionales",
    ar: "ترجمة قابلة للتبديل", fr: "Sous-titres activables",
    pt: "Legendas selecionáveis", ru: "Отключаемые субтитры",
  },
  "烧录方式": {
    en: "Subtitle output", hi: "उपशीर्षक आउटपुट", es: "Formato de subtítulos",
    ar: "نوع الترجمة", fr: "Type de sous-titres",
    pt: "Formato das legendas", ru: "Тип субтитров",
  },
  "确认后端已在运行：": {
    en: "Make sure the backend is running:", hi: "सुनिश्चित करें कि बैकएंड चल रहा है:",
    es: "Comprueba que el servidor esté en ejecución:", ar: "تأكد من تشغيل الخادم:",
    fr: "Vérifiez que le serveur fonctionne :",
    pt: "Verifique se o servidor está em execução:", ru: "Убедитесь, что сервер запущен:",
  },
};

const ENGLISH_OVERRIDES = {
  "快速开始": "Quick start", "MCP 调用": "MCP integration", "通知": "Notifications",
  "支持网站": "Supported sites", "源语言": "Source language", "目标语言": "Target language",
  "处理队列": "Processing queue", "翻译引擎": "Translation engine",
  "资源看板": "Storage dashboard", "字幕编辑": "Subtitle editor", "视频预览": "Video preview",
  "失败": "Failed", "软字幕（可开关）": "Switchable subtitles",
  "硬字幕": "Burned-in subtitles", "硬烧录": "Burned-in subtitles",
};

export function translate(source, selectedLocale = locale) {
  if (typeof source !== "string" || selectedLocale === "zh-CN") return source;
  const exact = MANUAL_TEXT[source]?.[selectedLocale]
    || (selectedLocale === "en" ? ENGLISH_OVERRIDES[source] : null)
    || LOCALIZED_TEXT[selectedLocale]?.[source];
  if (exact) return exact;
  if (source.endsWith(" · TranslatedSubs")) {
    return `${translate(source.slice(0, -" · TranslatedSubs".length), selectedLocale)} · TranslatedSubs`;
  }
  const pair = source.split(" → ");
  if (pair.length === 2 && pair.every((name) => LANGUAGE_CODES.has(name))) {
    try {
      const names = new Intl.DisplayNames([selectedLocale], { type: "language" });
      return pair.map((name) => names.of(LANGUAGE_CODES.get(name))).join(" → ");
    } catch (_) { /* use template fallback */ }
  }
  for (const [pattern, translated] of sortedTemplates[selectedLocale] || []) {
    let regex = templatePatterns.get(pattern);
    if (!regex) {
      regex = new RegExp("^" + pattern.split(/⟦\d+⟧/).map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("(.+?)") + "$");
      templatePatterns.set(pattern, regex);
    }
    const match = source.match(regex);
    if (match) return translated.replace(/⟦(\d+)⟧/g, (_, index) => match[Number(index)] || "");
  }
  return source;
}

function languageOption(option) {
  if (!option.parentElement?.matches("#sourceLang, #targetLang")) return null;
  if (option.value === "auto") return translate("自动检测");
  try {
    return new Intl.DisplayNames([locale], { type: "language" }).of(option.value) || option.value;
  } catch (_) {
    return option.value;
  }
}

function translateNode(node) {
  if (!node.parentElement || node.parentElement.closest("script, style, code, pre, textarea, [contenteditable], [data-i18n-ignore], #localePicker")) return;
  const current = node.nodeValue;
  if (!originals.has(node) || current !== lastRendered.get(node)) originals.set(node, current);
  const source = originals.get(node);
  const match = source.match(/^(\s*)([\s\S]*?)(\s*)$/);
  const translated = languageOption(node.parentElement)
    || translate(match[2]);
  const next = match[1] + translated + match[3];
  if (next !== current) node.nodeValue = next;
  lastRendered.set(node, next);
}

function translateAttributes(element) {
  if (element.closest("script, style, code, pre, [contenteditable], [data-i18n-ignore]")) return;
  let saved = attributeOriginals.get(element);
  let rendered = attributeRendered.get(element);
  if (!saved) {
    saved = new Map();
    rendered = new Map();
    attributeOriginals.set(element, saved);
    attributeRendered.set(element, rendered);
  }
  for (const name of ATTRIBUTES) {
    if (!element.hasAttribute(name)) continue;
    const current = element.getAttribute(name);
    if (!saved.has(name) || current !== rendered.get(name)) saved.set(name, current);
    const next = translate(saved.get(name));
    if (next !== current) element.setAttribute(name, next);
    rendered.set(name, next);
  }
}

function translateTree(root) {
  if (!root) return;
  if (root.nodeType === 3) {
    translateNode(root);
    return;
  }
  if (root.nodeType !== 1) return;
  translateAttributes(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    if (walker.currentNode.nodeType === 3) translateNode(walker.currentNode);
    else translateAttributes(walker.currentNode);
  }
}

export function setLocale(nextLocale, { persist = true } = {}) {
  const normalized = normalizeLocale(nextLocale);
  if (!normalized) return false;
  locale = normalized;
  if (persist) savePreference(locale);
  document.documentElement.lang = locale;
  document.documentElement.dir = locale === "ar" ? "rtl" : "ltr";
  const picker = document.getElementById("localePicker");
  if (picker) picker.value = locale;
  translateTree(document.documentElement);
  document.dispatchEvent(new CustomEvent("localechange", { detail: { locale } }));
  return true;
}

export function initI18n() {
  const picker = document.getElementById("localePicker");
  if (picker) {
    picker.replaceChildren(...Object.entries(UI_LOCALES).map(([code, name]) => {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = name;
      return option;
    }));
    picker.addEventListener("change", () => setLocale(picker.value));
  }
  setLocale(preferredLocale({
    saved: readPreference(),
    configured: window.APP_CONFIG?.UI_LOCALE,
    languages: navigator.languages || [navigator.language],
  }), { persist: false });
  observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") translateTree(mutation.target);
      else if (mutation.type === "attributes") translateAttributes(mutation.target);
      else for (const node of mutation.addedNodes) translateTree(node);
    }
  });
  observer.observe(document.documentElement, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ATTRIBUTES,
  });
  const nativeConfirm = window.confirm.bind(window);
  window.confirm = (message) => nativeConfirm(translate(String(message)));
}
