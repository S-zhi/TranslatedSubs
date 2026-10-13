import {
  SITE_CATEGORIES,
  categoryLabel,
  cloneDefaultSites,
} from "./developer-sites-data.js";

const STATUS_LABELS = Object.freeze({
  idle: "尚未验证",
  testing: "测试中",
  ok: "最近可抓取",
  fail: "异常 / 拦截",
});

export const BROWSER_COOKIE_SOURCES = Object.freeze([
  "chrome",
  "chromium",
  "edge",
  "firefox",
  "brave",
  "vivaldi",
  "opera",
  "safari",
]);

const STATUS_ICONS = Object.freeze({
  idle: "ph-minus-circle",
  testing: "ph-spinner-gap",
  ok: "ph-check-circle",
  fail: "ph-x-circle",
});

function finiteNumber(value, fallback = null) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function normalizeHostname(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  try {
    const candidate = raw.includes("://") ? raw : `https://${raw}`;
    return new URL(candidate).hostname.toLowerCase().replace(/\.$/, "");
  } catch (_) {
    return "";
  }
}

function probeRecordTimestamp(record) {
  const value = record && typeof record === "object" ? record.createdAt : null;
  if (typeof value === "number") {
    const timestamp = finiteNumber(value);
    return timestamp !== null && timestamp >= 0 ? timestamp : null;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  const numeric = finiteNumber(value.trim());
  if (numeric !== null) return numeric >= 0 ? numeric : null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function recordHostname(record) {
  if (!record || typeof record !== "object" || !isHttpUrl(record.url)) return "";
  return normalizeHostname(record.url);
}

function hostnameMatchesAlias(hostname, alias) {
  return Boolean(hostname && alias && (hostname === alias || hostname.endsWith(`.${alias}`)));
}

export function siteMatchesHostname(site, hostname) {
  const normalized = normalizeHostname(hostname);
  if (!normalized || !site || typeof site !== "object") return false;
  const aliases = Array.isArray(site.probeHostnames) && site.probeHostnames.length
    ? site.probeHostnames
    : [site.domain];
  return aliases
    .map(normalizeHostname)
    .filter(Boolean)
    .some((alias) => hostnameMatchesAlias(normalized, alias));
}

export function findLatestProbeRecord(site, records = []) {
  let latest = null;
  let latestTimestamp = null;
  for (const record of Array.isArray(records) ? records : []) {
    const timestamp = probeRecordTimestamp(record);
    if (timestamp === null || !recordHostname(record) || !siteMatchesHostname(site, record.url)) continue;
    if (latest === null || timestamp > latestTimestamp) {
      latest = record;
      latestTimestamp = timestamp;
    }
  }
  return latest;
}

export function restoreProbeHistory(sites = [], records = []) {
  return (Array.isArray(sites) ? sites : []).map((site) => {
    const next = { ...site, result: site.result ? { ...site.result } : null };
    if (site.status === "testing") return next;
    const latest = findLatestProbeRecord(site, records);
    if (!latest) return next;
    const result = normalizeProbeResult(latest);
    return {
      ...next,
      status: result.ok ? "ok" : "fail",
      result,
    };
  });
}

export function isHttpUrl(value) {
  try {
    const parsed = new URL(String(value || "").trim());
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch (_) {
    return false;
  }
}

export function siteMatchesQuery(site, query = "") {
  const normalized = String(query || "").trim().toLocaleLowerCase();
  if (!normalized) return true;
  return [site.name, site.domain, site.extractor, site.note, site.url]
    .filter(Boolean)
    .some((value) => String(value).toLocaleLowerCase().includes(normalized));
}

export function deriveSiteStats(sites = []) {
  return sites.reduce(
    (stats, site) => {
      stats.total += 1;
      if (site.status === "ok") stats.ok += 1;
      else if (site.status === "fail") stats.fail += 1;
      else if (site.status === "testing") stats.testing += 1;
      else stats.idle += 1;
      return stats;
    },
    { total: 0, ok: 0, fail: 0, idle: 0, testing: 0 },
  );
}

export function normalizeProbeResult(result = {}) {
  const value = result && typeof result === "object" ? result : {};
  const ok = Boolean(value.ok);
  const formats = Array.isArray(value.formats) ? value.formats : [];
  const availableQualities = Array.isArray(value.availableQualities)
    ? value.availableQualities.filter(Boolean).map(String)
    : [];
  const formatsCount = finiteNumber(value.formatsCount, formats.length) ?? 0;

  return {
    ok,
    title: value.title ? String(value.title) : null,
    extractor: value.extractor ? String(value.extractor) : null,
    duration: finiteNumber(value.duration),
    formatsCount,
    webpageUrl: value.webpageUrl ? String(value.webpageUrl) : null,
    reason: value.reason ? String(value.reason) : ok ? null : "yt-dlp 无法解析这个链接",
    detail: value.detail ? String(value.detail) : null,
    cached: Boolean(value.cached),
    language: value.language ? String(value.language) : null,
    availableQualities,
    formats,
    thumbnail: value.thumbnail ? String(value.thumbnail) : null,
    uploader: value.uploader ? String(value.uploader) : null,
  };
}

function createRuntimeSites() {
  return cloneDefaultSites().map((site) => ({
    ...site,
    selected: true,
    status: "idle",
    result: null,
    expanded: false,
  }));
}

function createElement(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function addIcon(doc, parent, iconName) {
  const icon = createElement(doc, "i", `ph ${iconName || "ph-broadcast"}`);
  icon.setAttribute("aria-hidden", "true");
  parent.append(icon);
  return icon;
}

function renderSiteLogo(doc, parent, site) {
  const fallback = createElement(doc, "span", "ytdlp-site-card__logo-fallback");
  addIcon(doc, fallback, site.icon || "ph-link");
  const logoUrl = typeof site.logoUrl === "string" && isHttpUrl(site.logoUrl)
    ? site.logoUrl
    : "";
  if (!logoUrl) {
    parent.append(fallback);
    return;
  }

  const image = createElement(doc, "img", "ytdlp-site-card__logo");
  image.alt = `${site.name} 网站图标`;
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.src = logoUrl;
  image.addEventListener("error", () => image.replaceWith(fallback), { once: true });
  parent.append(image);
}

function renderCookieTag(doc, site) {
  if (!site.cookieRequired && !site.cookieRecommended) return null;
  const tag = createElement(
    doc,
    "span",
    `ytdlp-tag ytdlp-tag--cookie${site.cookieRequired ? " is-required" : ""}`,
    site.cookieRequired ? "可能需要 Cookie" : "建议 Cookie",
  );
  tag.title = site.cookieNote || "可在本次探测中显式使用浏览器 Cookie";
  return tag;
}

function setText(root, selector, value) {
  const node = root.querySelector(selector);
  if (node) node.textContent = value;
  return node;
}

function formatDuration(seconds) {
  const duration = finiteNumber(seconds);
  if (duration === null || duration < 0) return "—";
  const total = Math.round(duration);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function setValidity(input, message) {
  if (!input) return false;
  input.setCustomValidity(message || "");
  if (message) input.reportValidity?.();
  return !message;
}

function appendMeta(doc, parent, label, value) {
  const item = createElement(doc, "div", "ytdlp-drawer__meta-item");
  item.append(
    createElement(doc, "span", "ytdlp-drawer__meta-k", label),
    createElement(doc, "span", "ytdlp-drawer__meta-v", value || "—"),
  );
  parent.append(item);
}

function renderDrawer(doc, site, drawer) {
  drawer.replaceChildren();
  if (site.status === "testing") {
    const loading = createElement(doc, "div", "ytdlp-drawer__loading");
    addIcon(doc, loading, "ph-spinner-gap");
    loading.append("正在请求 yt-dlp，请稍候…");
    drawer.append(loading);
    return;
  }

  if (!site.result) {
    drawer.append(createElement(doc, "div", "ytdlp-drawer__empty", "运行测试后，这里会显示提取器和格式诊断。"));
    return;
  }

  const result = site.result;
  if (!result.ok) {
    const alert = createElement(doc, "div", "ytdlp-diag-alert");
    const title = createElement(doc, "div", "ytdlp-diag-alert__title");
    addIcon(doc, title, "ph-warning-circle");
    title.append(result.reason || "解析失败");
    alert.append(title);
    if (site.note) alert.append(createElement(doc, "div", "ytdlp-diag-alert__tip", site.note));
    drawer.append(alert);

    if (result.detail) {
      const code = createElement(doc, "div", "ytdlp-diag-code");
      code.append(
        createElement(doc, "div", "ytdlp-diag-code__head", "yt-dlp detail"),
        createElement(doc, "pre", "", result.detail),
      );
      drawer.append(code);
    }
    return;
  }

  const meta = createElement(doc, "div", "ytdlp-drawer__meta-grid");
  appendMeta(doc, meta, "标题", result.title);
  appendMeta(doc, meta, "提取器", result.extractor || site.extractor);
  appendMeta(doc, meta, "时长", formatDuration(result.duration));
  appendMeta(doc, meta, "可用格式", result.formatsCount);
  if (result.uploader) appendMeta(doc, meta, "发布者", result.uploader);
  if (result.language) appendMeta(doc, meta, "语言", result.language);
  drawer.append(meta);

  const qualities = createElement(doc, "div", "ytdlp-drawer__qualities");
  qualities.append(createElement(doc, "span", "ytdlp-drawer__meta-k", "画质 / 音频"));
  const chips = createElement(doc, "div", "ytdlp-quality-chips");
  const values = result.availableQualities.length
    ? result.availableQualities
    : result.formatsCount
      ? [`${result.formatsCount} 种格式`]
      : ["未返回格式详情"];
  values.slice(0, 12).forEach((quality) => chips.append(createElement(doc, "span", "ytdlp-chip", quality)));
  qualities.append(chips);
  drawer.append(qualities);
}

function statusClass(status) {
  if (status === "ok") return "is-ok";
  if (status === "fail") return "is-fail";
  if (status === "testing") return "is-testing";
  return "";
}

function renderStatus(doc, site) {
  const status = createElement(doc, "span", `ytdlp-status-pill ytdlp-status-pill--${site.status}`);
  addIcon(doc, status, STATUS_ICONS[site.status] || STATUS_ICONS.idle);
  status.append(STATUS_LABELS[site.status] || STATUS_LABELS.idle);
  return status;
}

function renderSiteCard(doc, site, handlers) {
  const card = createElement(doc, "article", `ytdlp-site-card ${statusClass(site.status)}`.trim());
  card.dataset.siteId = site.id;

  const header = createElement(doc, "div", "ytdlp-site-card__header");
  const selectLabel = createElement(doc, "label", "ytdlp-site-card__select");
  const checkbox = createElement(doc, "input", "ytdlp-site-checkbox");
  checkbox.type = "checkbox";
  checkbox.checked = site.selected;
  checkbox.setAttribute("aria-label", `选择测试 ${site.name}`);
  checkbox.addEventListener("change", () => handlers.onSelect(site.id, checkbox.checked));

  const identity = createElement(doc, "span", "ytdlp-site-card__identity");
  const icon = createElement(doc, "span", "ytdlp-site-card__icon");
  renderSiteLogo(doc, icon, site);
  const naming = createElement(doc, "span", "ytdlp-site-card__naming");
  naming.append(
    createElement(doc, "strong", "ytdlp-site-card__name", site.name),
    createElement(doc, "span", "ytdlp-site-card__domain", site.domain),
  );
  identity.append(icon, naming);
  selectLabel.append(checkbox, identity);

  const badges = createElement(doc, "div", "ytdlp-site-card__badges");
  badges.append(createElement(doc, "span", "ytdlp-tag ytdlp-tag--extractor", site.extractor));
  badges.append(createElement(doc, `span`, `ytdlp-tag ${site.proxy ? "ytdlp-tag--proxy" : "ytdlp-tag--direct"}`, site.proxy ? "需代理" : "直连"));
  const cookieTag = renderCookieTag(doc, site);
  if (cookieTag) badges.append(cookieTag);
  badges.append(renderStatus(doc, site));
  header.append(selectLabel, badges);
  card.append(header);

  const urlRow = createElement(doc, "div", "ytdlp-site-card__url-row");
  const inputGroup = createElement(doc, "div", "ytdlp-input-group");
  const input = createElement(doc, "input", "ytdlp-url-input");
  input.type = "url";
  input.value = site.url || "";
  input.placeholder = "https://example.com/video/…";
  input.setAttribute("aria-label", `${site.name} 测试 URL`);
  input.addEventListener("input", () => {
    site.url = input.value;
    site.status = site.result ? site.status : "idle";
    handlers.onChange();
  });
  const copy = createElement(doc, "button", "ytdlp-btn-icon");
  copy.type = "button";
  copy.title = "复制 URL";
  copy.setAttribute("aria-label", `复制 ${site.name} URL`);
  addIcon(doc, copy, "ph-copy");
  copy.addEventListener("click", async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(site.url || "");
      else throw new Error("clipboard unavailable");
      copy.title = "已复制";
    } catch (_) {
      copy.title = "复制失败，请手动复制";
    }
  });
  inputGroup.append(input, copy);

  const run = createElement(doc, "button", "ytdlp-btn-action");
  run.type = "button";
  run.disabled = site.status === "testing" || !String(site.url || "").trim();
  addIcon(doc, run, site.status === "testing" ? "ph-spinner-gap" : "ph-play");
  run.append(site.status === "testing" ? "测试中" : "测试");
  run.addEventListener("click", () => handlers.onRun(site.id));
  urlRow.append(inputGroup, run);
  card.append(urlRow);

  const footer = createElement(doc, "div", "ytdlp-site-card__footer");
  const note = createElement(doc, "span", "ytdlp-site-card__note", site.note || categoryLabel(site.category));
  note.title = site.note || "";
  const expand = createElement(doc, "button", "ytdlp-btn-toggle-expand");
  expand.type = "button";
  expand.setAttribute("aria-expanded", site.expanded ? "true" : "false");
  addIcon(doc, expand, site.expanded ? "ph-caret-up" : "ph-caret-down");
  expand.append(site.expanded ? "收起诊断" : "查看诊断");
  expand.addEventListener("click", () => handlers.onExpand(site.id));
  footer.append(note, expand);
  card.append(footer);

  const drawer = createElement(doc, "div", `ytdlp-site-drawer${site.expanded ? " is-expanded" : ""}`);
  if (site.expanded) renderDrawer(doc, site, drawer);
  card.append(drawer);
  return card;
}

export function initDeveloperSitesView({ root = typeof document !== "undefined" ? document : null, api } = {}) {
  if (!root) return null;
  const grid = root.querySelector("#sitesGrid");
  if (!grid) return null;

  const doc = root.ownerDocument || root;
  const client = api || {};
  const sites = createRuntimeSites();
  let activeCategory = "all";
  let search = "";
  let batchRunning = false;
  let stopRequested = false;
  let environment = null;

  const getVisibleSites = () => sites.filter((site) => {
    const categoryMatches = activeCategory === "all" || site.category === activeCategory;
    return categoryMatches && siteMatchesQuery(site, search);
  });

  const updateCookieControls = () => {
    const checkbox = root.querySelector("#browserCookiesCheck");
    const select = root.querySelector("#browserCookiesSelect");
    const help = root.querySelector("#browserCookiesHelp");
    const enabled = Boolean(environment?.browserCookiesEnabled);
    if (checkbox) {
      checkbox.disabled = !enabled;
      if (!enabled) checkbox.checked = false;
    }
    if (select) {
      select.disabled = !enabled || !checkbox?.checked;
      if (!enabled) select.value = "";
    }
    if (help) {
      help.textContent = enabled
        ? "仅影响当前 probe；Cookie 不会上传或保存"
        : "后端未启用浏览器 Cookie，默认不会读取本机登录态";
      help.classList.toggle("is-enabled", enabled);
      help.classList.toggle("is-disabled", !enabled);
    }
  };

  const getProbeOptions = () => {
    const checkbox = root.querySelector("#browserCookiesCheck");
    if (!checkbox?.checked) return {};
    if (!environment?.browserCookiesEnabled) return null;
    const browser = root.querySelector("#browserCookiesSelect")?.value;
    if (!BROWSER_COOKIE_SOURCES.includes(browser)) return null;
    return { cookiesFromBrowser: browser };
  };

  const renderStats = () => {
    const stats = deriveSiteStats(sites);
    setText(root, "#statTotal", stats.total);
    setText(root, "#statOk", stats.ok);
    setText(root, "#statFail", stats.fail);
    setText(root, "#statIdle", stats.idle);
  };

  const renderCategories = () => {
    const categories = root.querySelector("#sitesCategories");
    if (!categories) return;
    categories.replaceChildren();
    SITE_CATEGORIES.forEach((category) => {
      const button = createElement(doc, "button", `developer-filter${activeCategory === category.id ? " is-active" : ""}`, category.label);
      button.type = "button";
      button.dataset.category = category.id;
      button.setAttribute("aria-pressed", activeCategory === category.id ? "true" : "false");
      button.addEventListener("click", () => {
        activeCategory = category.id;
        renderCategories();
        renderSites();
      });
      categories.append(button);
    });
  };

  const renderSites = () => {
    grid.replaceChildren();
    const visible = getVisibleSites();
    if (!visible.length) {
      const empty = createElement(doc, "div", "ytdlp-sites-empty");
      addIcon(doc, empty, "ph-magnifying-glass");
      empty.append("没有匹配的网站");
      grid.append(empty);
      return;
    }

    const handlers = {
      onSelect: (id, selected) => {
        const site = sites.find((item) => item.id === id);
        if (site) site.selected = selected;
        updateBatchControls();
      },
      onChange: () => {
        renderStats();
        updateBatchControls();
      },
      onRun: (id) => { void runSite(id); },
      onExpand: (id) => {
        const site = sites.find((item) => item.id === id);
        if (!site) return;
        site.expanded = !site.expanded;
        renderSites();
      },
    };
    visible.forEach((site) => grid.append(renderSiteCard(doc, site, handlers)));
  };

  const updateBatchControls = () => {
    const runButton = root.querySelector("#btnBatchRun");
    const stopButton = root.querySelector("#btnBatchStop");
    const selectedCount = sites.filter((site) => site.selected && String(site.url || "").trim()).length;
    if (runButton) {
      runButton.disabled = batchRunning || selectedCount === 0;
      runButton.title = selectedCount ? `已选择 ${selectedCount} 个网站` : "请先选择至少一个有效链接";
    }
    if (stopButton) stopButton.style.display = batchRunning ? "inline-flex" : "none";
  };

  const runSite = async (id) => {
    const site = sites.find((item) => item.id === id);
    if (!site || site.status === "testing") return null;
    const url = String(site.url || "").trim();
    if (!isHttpUrl(url)) {
      site.status = "fail";
      site.result = normalizeProbeResult({ ok: false, reason: "请输入有效的视频链接" });
      site.expanded = true;
      renderStats();
      renderSites();
      return site.result;
    }

    const probeOptions = getProbeOptions();
    if (probeOptions === null) {
      const help = root.querySelector("#browserCookiesHelp");
      if (help) {
        help.textContent = "已勾选浏览器 Cookie，请先选择来源浏览器";
        help.classList.add("is-disabled");
      }
      return null;
    }

    site.status = "testing";
    site.result = null;
    renderStats();
    renderSites();
    try {
      if (typeof client.probeVideo !== "function") throw new Error("探测 API 不可用");
      const result = normalizeProbeResult(await client.probeVideo(url, probeOptions));
      site.result = result;
      site.status = result.ok ? "ok" : "fail";
    } catch (error) {
      site.status = "fail";
      site.result = normalizeProbeResult({
        ok: false,
        reason: "链接探测失败",
        detail: error?.message || String(error),
      });
    }
    site.expanded = true;
    renderStats();
    renderSites();
    return site.result;
  };

  const runSelected = async () => {
    if (batchRunning) return;
    const selected = sites.filter((site) => site.selected && String(site.url || "").trim());
    if (!selected.length) return;
    batchRunning = true;
    stopRequested = false;
    updateBatchControls();
    try {
      for (const site of selected) {
        if (stopRequested) break;
        await runSite(site.id);
      }
    } finally {
      batchRunning = false;
      stopRequested = false;
      updateBatchControls();
    }
  };

  const loadEnvironment = async () => {
    const versionNode = root.querySelector("#statEnvYtdlp");
    const proxyNode = root.querySelector("#statEnvProxy");
    try {
      if (typeof client.getYtDlpInfo !== "function") throw new Error("环境信息 API 不可用");
      environment = await client.getYtDlpInfo();
      const version = environment?.version ? `yt-dlp ${environment.version}` : "yt-dlp 未知版本";
      if (versionNode) {
        versionNode.textContent = version;
        versionNode.classList.toggle("is-active", Boolean(environment?.version));
      }
      if (proxyNode) {
        const proxy = environment?.proxyConfigured
          ? environment.proxyMasked || "已配置代理"
          : "直连无代理";
        proxyNode.textContent = proxy;
        proxyNode.classList.toggle("is-active", Boolean(environment?.proxyConfigured));
        proxyNode.title = environment?.cookiesConfigured ? "cookies 已配置" : "cookies 未配置";
      }
    } catch (_) {
      if (versionNode) versionNode.textContent = "读取失败";
      if (proxyNode) proxyNode.textContent = "环境未知";
    }
    updateCookieControls();
    return environment;
  };

  const applyStartupStatus = (startup) => {
    if (!startup || !Array.isArray(startup.sites)) return false;
    const fixedSites = new Map(sites.filter((site) => !site.id.startsWith("custom-")).map((site) => [site.id, site]));
    for (const item of startup.sites) {
      const site = fixedSites.get(item.id);
      if (!site) continue;
      if (item.status === "ok" || item.status === "fail") {
        const result = normalizeProbeResult(item.result || { ok: item.status === "ok" });
        site.status = result.ok ? "ok" : "fail";
        site.result = result;
      } else if (startup.state === "running") {
        site.status = "testing";
        site.result = null;
      } else {
        site.status = "idle";
        site.result = null;
      }
    }
    return true;
  };

  const loadProbeHistory = async () => {
    const statusNode = root.querySelector("#sitesHistoryStatus");
    let records = [];
    let historyLoaded = false;
    try {
      if (typeof client.listProbeRecords !== "function") throw new Error("探测历史 API 不可用");
      records = await client.listProbeRecords(500);
      const restored = restoreProbeHistory(sites, records);
      sites.splice(0, sites.length, ...restored);
      historyLoaded = true;
    } catch (_) {
      if (statusNode) {
        statusNode.textContent = "暂时无法读取主验证页探测历史，当前仅显示本次页面状态。";
        statusNode.classList.add("is-error");
      }
    }

    let startup = null;
    try {
      if (typeof client.getProbeStartupStatus === "function") {
        startup = await client.getProbeStartupStatus();
      }
    } catch (_) {
      startup = null;
    }

    const startupApplied = applyStartupStatus(startup);
    if (statusNode && startupApplied) {
      if (startup.state === "running") {
        statusNode.textContent = `启动探测进行中：${startup.completed || 0} / ${startup.total || sites.length} 个站点已完成。`;
      } else if (startup.state === "completed") {
        statusNode.textContent = `已同步当前启动批次：${startup.successful || 0} / ${startup.total || sites.length} 个站点当前可抓取。`;
      } else if (startup.state === "disabled") {
        statusNode.textContent = "后端已关闭自动启动探测，当前显示历史结果。";
      }
      statusNode.classList.remove("is-error");
    } else if (statusNode && historyLoaded) {
      const count = Array.isArray(records) ? records.length : 0;
      statusNode.textContent = count
        ? `已同步主验证页最近 ${count} 条探测记录，卡片状态以最新结果为准。`
        : "主验证页暂无探测记录；运行测试后会显示最近可抓取状态。";
      statusNode.classList.remove("is-error");
    }

    renderStats();
    renderSites();
    updateBatchControls();
  };

  const openModal = () => {
    const modal = root.querySelector("#customSiteModal");
    if (!modal) return;
    modal.classList.remove("is-hidden");
    root.querySelector("#customUrlInput")?.focus();
  };

  const closeModal = () => {
    const modal = root.querySelector("#customSiteModal");
    if (!modal) return;
    modal.classList.add("is-hidden");
    root.querySelector("#customSiteForm")?.reset();
    setValidity(root.querySelector("#customUrlInput"), "");
  };

  root.querySelector("#browserCookiesCheck")?.addEventListener("change", () => {
    updateCookieControls();
  });
  root.querySelector("#browserCookiesSelect")?.addEventListener("change", () => {
    updateCookieControls();
  });
  root.querySelector("#btnBatchRun")?.addEventListener("click", () => { void runSelected(); });
  root.querySelector("#btnBatchStop")?.addEventListener("click", () => { stopRequested = true; });
  root.querySelector("#btnSelectAll")?.addEventListener("click", () => {
    const allSelected = sites.length > 0 && sites.every((site) => site.selected);
    sites.forEach((site) => { site.selected = !allSelected; });
    renderSites();
    updateBatchControls();
  });
  root.querySelector("#btnResetAll")?.addEventListener("click", () => {
    sites.forEach((site) => {
      site.status = "idle";
      site.result = null;
      site.expanded = false;
    });
    renderStats();
    renderSites();
    updateBatchControls();
  });
  root.querySelector("#sitesSearchInput")?.addEventListener("input", (event) => {
    search = event.target.value;
    renderSites();
  });
  root.querySelector("#btnAddCustomModal")?.addEventListener("click", openModal);
  root.querySelector("#btnCloseCustomModal")?.addEventListener("click", closeModal);
  root.querySelector("#customSiteModal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeModal();
  });
  root.querySelector("#customSiteForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const nameInput = root.querySelector("#customNameInput");
    const urlInput = root.querySelector("#customUrlInput");
    const categoryInput = root.querySelector("#customCatSelect");
    const proxyInput = root.querySelector("#customProxyCheck");
    const url = String(urlInput?.value || "").trim();
    if (!setValidity(urlInput, isHttpUrl(url) ? "" : "请输入 http:// 或 https:// 视频链接")) return;

    let parsed;
    try { parsed = new URL(url); } catch (_) { return; }
    const suffix = Math.random().toString(36).slice(2, 8);
    sites.push({
      id: `custom-${Date.now()}-${suffix}`,
      name: String(nameInput?.value || "").trim() || parsed.hostname,
      domain: parsed.hostname,
      probeHostnames: [parsed.hostname],
      category: categoryInput?.value || "international",
      extractor: "Custom",
      icon: "ph-link",
      url,
      proxy: Boolean(proxyInput?.checked),
      note: "自定义测试链接",
      selected: true,
      status: "idle",
      result: null,
      expanded: false,
    });
    closeModal();
    renderStats();
    renderSites();
    updateBatchControls();
  });
  root.querySelector("#btnExportReport")?.addEventListener("click", () => {
    const report = {
      generatedAt: new Date().toISOString(),
      environment,
      stats: deriveSiteStats(sites),
      sites: sites.map((site) => ({
        id: site.id,
        name: site.name,
        domain: site.domain,
        category: site.category,
        extractor: site.extractor,
        url: site.url,
        proxy: site.proxy,
        status: site.status,
        result: site.result,
      })),
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const href = URL.createObjectURL(blob);
    const link = createElement(doc, "a");
    link.href = href;
    link.download = `ytdlp-sites-report-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(href), 0);
  });

  const view = {
    ready: null,
    refresh() {
      renderStats();
      renderCategories();
      renderSites();
      updateBatchControls();
    },
    getSites() {
      return sites.map((site) => ({ ...site, result: site.result ? { ...site.result } : null }));
    },
    runSite,
    stop() { stopRequested = true; },
  };
  view.refresh();
  updateCookieControls();
  view.ready = Promise.all([loadEnvironment(), loadProbeHistory()]);
  return view;
}
