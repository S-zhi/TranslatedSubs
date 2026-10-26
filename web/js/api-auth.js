/* Backend credentials stay in this tab's session and are scoped to its API base. */

const tokens = new Map();

function storageKey(base) {
  return `subtrans:api-token:${String(base).replace(/\/+$/, "")}`;
}

export function getApiToken(base) {
  const key = storageKey(base);
  if (tokens.has(key)) return tokens.get(key);
  try {
    return window.sessionStorage?.getItem(key) || "";
  } catch {
    return "";
  }
}

export function setApiToken(base, value) {
  const key = storageKey(base);
  const token = String(value || "").trim();
  tokens.set(key, token);
  try {
    if (token) window.sessionStorage?.setItem(key, token);
    else window.sessionStorage?.removeItem(key);
  } catch {
    // Keep the connection usable when browser storage is disabled.
  }
}

// Consume the launch URL before any initializer can issue a protected request.
export function consumeTrialTokenFragment(base) {
  if (typeof window === "undefined" || !window.location) return false;
  const { location, history } = window;
  const fragment = String(location.hash || "");
  const decodedFragment = fragment.replace(
    /%([0-9a-f]{2})/gi,
    (_, hex) => String.fromCharCode(Number.parseInt(hex, 16)),
  );
  if (!/trial_token/i.test(decodedFragment)) return false;

  const match = /^#trial_token=([0-9a-f]{64})$/.exec(fragment);
  const apiBase = base ?? window.APP_CONFIG?.API_BASE_URL ?? location.origin;
  let acceptableOrigin = false;
  try {
    const page = new URL(location.href);
    const api = typeof apiBase === "string" && apiBase ? new URL(apiBase) : null;
    const loopbackIPv4 = /^127(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/.test(page.hostname);
    const loopback = page.hostname === "localhost" || page.hostname === "[::1]" || loopbackIPv4;
    acceptableOrigin = (page.protocol === "https:" || (page.protocol === "http:" && loopback))
      && api !== null
      && ["https:", "http:"].includes(api.protocol)
      && api.origin === page.origin;
  } catch {
    acceptableOrigin = false;
  }

  // Scrub rejected fragments too; preserve the URL's own scheme for opaque origins.
  try {
    const scrubbedUrl = new URL(location.href);
    scrubbedUrl.hash = "";
    const historyUrl = ["https:", "http:"].includes(scrubbedUrl.protocol)
      ? `${location.pathname}${location.search}`
      : scrubbedUrl.href;
    history.replaceState(history.state, "", historyUrl);
  } catch {
    return false;
  }

  if (!match || match[0] !== fragment || !acceptableOrigin || typeof apiBase !== "string") return false;
  setApiToken(apiBase, match[1]);
  return true;
}

export function apiAuthHeaders(base, headers) {
  const token = getApiToken(base);
  if (!token) return headers;
  const result = new Headers(headers);
  result.set("Authorization", `Bearer ${token}`);
  return Object.fromEntries(result.entries());
}

export function authenticatedApiUrl(base, path) {
  const url = `${base}${path}`;
  const token = getApiToken(base);
  if (!token) return url;
  const result = new URL(url, typeof window !== "undefined" ? window.location?.href : undefined);
  result.searchParams.set("token", token);
  return result.href;
}
