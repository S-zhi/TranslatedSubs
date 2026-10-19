import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";
import { consumeTrialTokenFragment, getApiToken, setApiToken } from "../js/api-auth.js";

const configSource = readFileSync(new URL("../config.js", import.meta.url), "utf8");
const mainSource = readFileSync(new URL("../js/main.js", import.meta.url), "utf8");
const token = "0123456789abcdef".repeat(4);
let nextPage = 0;
let nextApi = 0;

function trialPage(fragment, { pageUrl, override = "https://old-api.example", blockedStorage = false } = {}) {
  const location = new URL(pageUrl || `https://trial-${++nextPage}.example/settings?tab=plugins`);
  location.hash = fragment;
  const localValues = new Map([["SUBTRANS_API_BASE_URL", override]]);
  const sessionValues = new Map();
  const calls = { localReads: 0, localRemovals: 0, sessionWrites: 0, history: [] };
  const window = {
    location,
    addEventListener() {},
    history: {
      state: { view: "other-settings" },
      replaceState(state, unused, url) {
        calls.history.push({ state, unused, url });
        location.href = new URL(url, location).href;
      },
    },
  };
  Object.defineProperty(window, "localStorage", {
    get() {
      if (blockedStorage) throw new Error("storage blocked");
      return {
        getItem(key) { calls.localReads++; return localValues.get(key) || null; },
        removeItem(key) { calls.localRemovals++; localValues.delete(key); },
      };
    },
  });
  Object.defineProperty(window, "sessionStorage", {
    get() {
      if (blockedStorage) throw new Error("storage blocked");
      return {
        getItem: (key) => sessionValues.get(key) || null,
        setItem(key, value) { calls.sessionWrites++; sessionValues.set(key, value); },
        removeItem: (key) => sessionValues.delete(key),
      };
    },
  });
  const context = { window };
  Object.defineProperty(context, "localStorage", { get: () => window.localStorage });
  runInNewContext(configSource, context);
  return { window, calls, localValues, sessionValues };
}

async function loadApi(window) {
  globalThis.window = window;
  return (await import(`../js/api.js?trial=${++nextApi}`)).Api;
}

function captureRequests() {
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ revision: 0, instances: [] }) };
  };
  return requests;
}

test("trial boot pins the captured API origin and authenticates its first initializer request", async () => {
  const { window, calls, localValues, sessionValues } = trialPage(`#trial_token=${token}`);
  const savedWindow = globalThis.window;
  const savedFetch = globalThis.fetch;
  try {
    const Api = await loadApi(window);
    const requests = captureRequests();
    const initialState = window.history.state;
    assert.equal(Api.base, window.location.origin);
    assert.equal(calls.localReads, 0);
    assert.equal(calls.localRemovals, 1);
    assert.equal(localValues.has("SUBTRANS_API_BASE_URL"), false);
    assert.equal(requests.length, 0);

    const imports = [...mainSource.matchAll(/import \{([^}]+)\} from [^;]+;/g)]
      .flatMap((match) => match[1].split(",").map((name) => name.trim()));
    const bootCalls = [];
    const pending = [];
    const context = Object.fromEntries(imports.map((name) => [name, () => bootCalls.push(name)]));
    Object.assign(context, {
      Api, window, document: {},
      isDesktop: () => false,
      consumeTrialTokenFragment(base) {
        bootCalls.push("consumeTrialTokenFragment");
        assert.equal(consumeTrialTokenFragment(base), true);
      },
      initApiConnection() {
        bootCalls.push("initApiConnection");
        assert.equal(getApiToken(Api.base), token);
        pending.push(Api.listPluginInstances());
      },
    });
    runInNewContext(mainSource.replace(/import \{[^}]+\} from [^;]+;/g, ""), context);
    await Promise.all(pending);

    assert.deepEqual(bootCalls.slice(0, 2), ["consumeTrialTokenFragment", "initApiConnection"]);
    assert.equal(requests.length, 1);
    assert.equal(new URL(requests[0].url).origin, window.location.origin);
    assert.equal(requests[0].options.headers.authorization, `Bearer ${token}`);
    assert.equal(sessionValues.get(`subtrans:api-token:${Api.base}`), token);
    assert.equal(window.location.hash, "");
    assert.equal(window.location.pathname, "/settings");
    assert.equal(window.location.search, "?tab=plugins");
    assert.deepEqual(calls.history, [{ state: initialState, unused: "", url: "/settings?tab=plugins" }]);
    assert.equal(consumeTrialTokenFragment(Api.base), false);
    assert.equal(calls.history.length, 1);
    assert.equal(calls.sessionWrites, 1);
  } finally {
    setApiToken(window.APP_CONFIG.API_BASE_URL, "");
    globalThis.window = savedWindow;
    globalThis.fetch = savedFetch;
  }
});

test("trial handoff works in memory when localStorage and sessionStorage access are blocked", async () => {
  const { window, calls } = trialPage(`#trial_token=${token}`, { blockedStorage: true });
  const savedWindow = globalThis.window;
  const savedFetch = globalThis.fetch;
  try {
    const Api = await loadApi(window);
    const requests = captureRequests();
    assert.equal(Api.base, window.location.origin);
    assert.equal(consumeTrialTokenFragment(Api.base), true);
    assert.equal(getApiToken(Api.base), token);
    await Api.listPluginInstances();
    assert.equal(requests[0].options.headers.authorization, `Bearer ${token}`);
    assert.equal(window.location.hash, "");
    assert.equal(consumeTrialTokenFragment(Api.base), false);
    assert.equal(calls.history.length, 1);
  } finally {
    setApiToken(window.APP_CONFIG.API_BASE_URL, "");
    globalThis.window = savedWindow;
    globalThis.fetch = savedFetch;
  }
});

test("duplicate, extra and malformed trial fragments are scrubbed without replacing credentials", async (t) => {
  const fragments = [
    "#trial_token=",
    "#trial_token=short",
    `#trial_token=${token.toUpperCase()}`,
    `#trial_token=${token.slice(1)}`,
    `#trial_token=${token}a`,
    `#trial_token=${token}&trial_token=${token}`,
    `#trial_token=${token}&other=1`,
    `#other=1&trial_token=${token}`,
    `#trial_token=${token}&`,
    `#trial_token=${token}#next`,
    `#trial_token=${token}%0A`,
    `#trial_token=${token}%61`,
    `#trial_token=%30${token.slice(1)}`,
    `#%74rial_token=${token}`,
    `#?trial_token=${token}`,
    `#TRIAL_TOKEN=${token}`,
    "#trial_token",
  ];
  for (const fragment of fragments) {
    await t.test(fragment, () => {
      const { window, calls, localValues } = trialPage(fragment);
      const savedWindow = globalThis.window;
      globalThis.window = window;
      const base = window.location.origin;
      try {
        setApiToken(base, "existing-manual-token");
        assert.equal(window.APP_CONFIG.API_BASE_URL, base);
        assert.equal(localValues.has("SUBTRANS_API_BASE_URL"), false);
        assert.equal(consumeTrialTokenFragment(base), false);
        assert.equal(getApiToken(base), "existing-manual-token");
        assert.equal(calls.sessionWrites, 1);
        assert.equal(window.location.hash, "");
        assert.equal(calls.history.length, 1);
        assert.equal(consumeTrialTokenFragment(base), false);
        assert.equal(calls.history.length, 1);
      } finally {
        setApiToken(base, "");
        globalThis.window = savedWindow;
      }
    });
  }
});

test("trial credentials reject cross-origin API bases, plaintext remote pages and non-HTTP origins", async (t) => {
  const cases = [
    ["https://cross-origin.example/", "https://other-api.example"],
    ["https://protocol.example/", "http://protocol.example"],
    ["https://port.example/", "https://port.example:8443"],
    ["https://relative-cross.example/", "//other-api.example"],
    ["http://plaintext.example/", "http://plaintext.example"],
    ["file:///tmp/trial/index.html", "file:///tmp/trial"],
    ["ftp://trial.example/", "ftp://trial.example"],
    ["blob:https://blob-page.example/id", "https://blob-page.example"],
    ["https://blob-api.example/", "blob:https://blob-api.example/id"],
    ["https://invalid-base.example/", ""],
    ["https://invalid-type.example/", 42],
  ];
  for (const [pageUrl, base] of cases) {
    await t.test(`${pageUrl} -> ${base}`, () => {
      const { window } = trialPage(`#trial_token=${token}`, { pageUrl });
      const savedWindow = globalThis.window;
      globalThis.window = window;
      try {
        assert.equal(consumeTrialTokenFragment(base), false);
        assert.equal(getApiToken(base), "");
        assert.equal(getApiToken(window.location.origin), "");
      } finally {
        globalThis.window = savedWindow;
      }
    });
  }
});

test("loopback HTTP trial previews pin the page port rather than the local API default", () => {
  const savedWindow = globalThis.window;
  for (const pageUrl of ["http://localhost:5273/", "http://127.0.0.1:8000/", "http://[::1]:8000/"]) {
    const { window } = trialPage(`#trial_token=${token}`, { pageUrl });
    globalThis.window = window;
    try {
      assert.equal(window.APP_CONFIG.API_BASE_URL, window.location.origin);
      assert.equal(consumeTrialTokenFragment(window.APP_CONFIG.API_BASE_URL), true);
      assert.equal(getApiToken(window.location.origin), token);
    } finally {
      setApiToken(window.location.origin, "");
      globalThis.window = savedWindow;
    }
  }
});

test("ordinary fragments retain manual authentication and saved external API overrides", () => {
  const savedWindow = globalThis.window;
  for (const fragment of ["", "#preview"]) {
    const { window, calls, localValues } = trialPage(fragment);
    globalThis.window = window;
    const base = window.APP_CONFIG.API_BASE_URL;
    try {
      setApiToken(base, "manual-token");
      assert.equal(base, "https://old-api.example");
      assert.equal(consumeTrialTokenFragment(base), false);
      assert.equal(getApiToken(base), "manual-token");
      assert.equal(window.location.hash, fragment);
      assert.equal(calls.localReads, 1);
      assert.equal(calls.localRemovals, 0);
      assert.equal(localValues.get("SUBTRANS_API_BASE_URL"), base);
      assert.equal(calls.history.length, 0);
    } finally {
      setApiToken(base, "");
      globalThis.window = savedWindow;
    }
  }
});
