import { test } from "node:test";
import assert from "node:assert/strict";
import { LANG_LABEL } from "../js/constants.js";

globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: true,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  },
};

const { Api } = await import("../js/api.js");

test("LANG_LABEL retains labels for existing tasks", () => {
  const targetLangs = Object.keys(LANG_LABEL).filter(
    (k) => k !== "auto" && k !== "zh"
  );
  assert.equal(targetLangs.length, 40);
  assert.ok(targetLangs.includes("zh-CN"));
  assert.ok(targetLangs.includes("es"));
  assert.ok(targetLangs.includes("fr"));
  assert.ok(targetLangs.includes("de"));
  assert.ok(targetLangs.includes("sw"));
  assert.ok(targetLangs.includes("bn"));
  assert.ok(targetLangs.includes("ur"));
});

test("MockApi.listTargetLanguages excludes Bengali and Urdu", async () => {
  const langs = await Api.listTargetLanguages();
  assert.equal(langs.length, 38);
  assert.ok(langs.includes("zh-CN"));
  assert.ok(langs.includes("es"));
  assert.ok(langs.includes("fr"));
  assert.ok(langs.includes("de"));
  assert.ok(langs.includes("sw"));
  assert.ok(!langs.includes("bn"));
  assert.ok(!langs.includes("ur"));
});

test("MockApi.listVideoLanguages excludes Bengali and Urdu", async () => {
  const langs = await Api.listVideoLanguages();
  assert.ok(langs.includes("hi"));
  assert.ok(!langs.includes("bn"));
  assert.ok(!langs.includes("ur"));
});
