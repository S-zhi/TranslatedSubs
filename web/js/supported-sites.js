import { DEFAULT_SITES } from "./developer-sites-data.js";
import { Api } from "./api.js";

const POLL_INTERVAL_MS = 5000;

function createElement(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

export function successfulSitesFromStatus(status, sites = DEFAULT_SITES) {
  const byId = new Map(
    (Array.isArray(status?.sites) ? status.sites : []).map((site) => [site.id, site]),
  );
  return sites.filter((site) => byId.get(site.id)?.status === "ok");
}

function renderLogo(doc, parent, site) {
  const image = createElement(doc, "img");
  image.src = site.logoUrl;
  image.alt = "";
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => {
    const fallback = createElement(doc, "span", "site-icon__fallback", site.name.slice(0, 1));
    image.replaceWith(fallback);
  }, { once: true });
  parent.append(image);
}

function renderSite(doc, site) {
  const link = createElement(doc, "a", `site-icon site-icon--${site.id}`);
  link.href = `https://${site.domain}`;
  link.target = "_blank";
  link.rel = "noreferrer";
  link.title = `${site.name} · 当前可抓取`;
  link.setAttribute("aria-label", `打开 ${site.name}`);
  const mark = createElement(doc, "span", "site-icon__mark");
  renderLogo(doc, mark, site);
  link.append(mark, createElement(doc, "span", "site-icon__label", site.name));
  return link;
}

function renderPlaceholder(doc, track, status) {
  const placeholder = createElement(doc, "div", "supported-sites__placeholder");
  const loader = createElement(doc, "span", "supported-sites__loader", "");
  loader.setAttribute("aria-hidden", "true");
  loader.append(
    createElement(doc, "i"),
    createElement(doc, "i"),
    createElement(doc, "i"),
  );
  const text = status?.state === "idle" || status?.state === "disabled"
    ? "等待后端检测"
    : `正在检测 ${status?.completed || 0} / ${status?.total || DEFAULT_SITES.length}`;
  placeholder.append(loader, createElement(doc, "span", "supported-sites__placeholder-text", text));
  track.append(placeholder);
}

function renderEmpty(doc, track) {
  const empty = createElement(doc, "div", "supported-sites__empty", "当前暂无可抓取站点");
  track.append(empty);
}

function renderTrack(doc, track, status) {
  track.replaceChildren();
  track.classList.remove("is-static");
  if (!status || status.state !== "completed") {
    renderPlaceholder(doc, track, status);
    track.classList.add("is-static");
    return;
  }

  const available = successfulSitesFromStatus(status);
  if (!available.length) {
    renderEmpty(doc, track);
    track.classList.add("is-static");
    return;
  }

  const firstGroup = createElement(doc, "div", "supported-sites__group");
  available.forEach((site) => firstGroup.append(renderSite(doc, site)));
  track.append(firstGroup);

  if (available.length >= 2) {
    const secondGroup = createElement(doc, "div", "supported-sites__group");
    secondGroup.setAttribute("aria-hidden", "true");
    secondGroup.inert = true;
    available.forEach((site) => {
      const clone = renderSite(doc, site);
      clone.setAttribute("tabindex", "-1");
      clone.setAttribute("aria-hidden", "true");
      secondGroup.append(clone);
    });
    track.append(secondGroup);
  } else {
    track.classList.add("is-static");
  }
}

function renderStatus(root, status) {
  const count = root.querySelector("#supportedSitesCount");
  if (!count) return;
  if (!status || status.state !== "completed") {
    count.textContent = !status
      ? "等待后端检测"
      : status.state === "disabled"
        ? "自动检测已关闭"
        : `正在检测 ${status.completed || 0} / ${status.total || DEFAULT_SITES.length}`;
    return;
  }
  count.textContent = `${status.successful || 0} / ${status.total || DEFAULT_SITES.length} 当前可抓取`;
}

export function initSupportedSitesView({ root = typeof document !== "undefined" ? document : null, api = Api } = {}) {
  if (!root) return null;
  const track = root.querySelector("#supportedSitesTrack");
  if (!track) return null;
  const doc = root.ownerDocument || root;
  let stopped = false;
  let loading = false;
  let timer = null;
  let currentStatus = null;

  const render = (status) => {
    currentStatus = status;
    renderStatus(root, status);
    renderTrack(doc, track, status);
  };

  const refresh = async () => {
    if (stopped || loading || typeof api.getProbeStartupStatus !== "function") return currentStatus;
    if (typeof document !== "undefined" && document.hidden) return currentStatus;
    loading = true;
    try {
      render(await api.getProbeStartupStatus());
    } catch (_) {
      render(null);
    } finally {
      loading = false;
    }
    return currentStatus;
  };

  render(null);
  void refresh();
  timer = setInterval(() => { void refresh(); }, POLL_INTERVAL_MS);
  root.defaultView?.addEventListener("focus", refresh);
  root.defaultView?.addEventListener("online", refresh);
  root.defaultView?.addEventListener("beforeunload", () => {
    stopped = true;
    if (timer) clearInterval(timer);
  }, { once: true });

  return {
    refresh,
    stop() {
      stopped = true;
      if (timer) clearInterval(timer);
    },
    getStatus() {
      return currentStatus;
    },
  };
}
