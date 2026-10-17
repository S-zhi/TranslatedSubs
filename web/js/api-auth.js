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
