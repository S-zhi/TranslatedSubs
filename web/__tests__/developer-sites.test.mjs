import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_SITES,
  SITE_CATEGORIES,
  categoryLabel,
} from "../js/developer-sites-data.js";
import {
  findLatestProbeRecord,
  deriveSiteStats,
  isHttpUrl,
  normalizeHostname,
  normalizeProbeResult,
  restoreProbeHistory,
  siteMatchesHostname,
  siteMatchesQuery,
} from "../js/developer-sites.js";

test("developer site registry has unique ids and complete probe metadata", () => {
  const ids = DEFAULT_SITES.map((site) => site.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const site of DEFAULT_SITES) {
    assert.ok(site.name);
    assert.ok(site.domain);
    assert.ok(site.category);
    assert.ok(site.extractor);
    assert.ok(site.url);
    assert.ok(isHttpUrl(site.url));
    assert.ok(SITE_CATEGORIES.some((category) => category.id === site.category));
  }
});

test("site registry keeps the current ten sites and safe logo/cookie metadata", () => {
  assert.deepEqual(DEFAULT_SITES.map((site) => site.id), [
    "youtube",
    "vimeo",
    "dailymotion",
    "twitch",
    "tiktok",
    "twitter",
    "instagram",
    "acfun",
    "niconico",
    "pornhub",
  ]);
  assert.equal(DEFAULT_SITES.some((site) => site.id === "bilibili"), false);
  for (const site of DEFAULT_SITES) {
    assert.equal(isHttpUrl(site.logoUrl), true);
    assert.ok(Array.isArray(site.probeHostnames));
    assert.equal(typeof site.cookieRequired, "boolean");
    assert.equal(typeof site.cookieRecommended, "boolean");
    assert.equal(site.logoUrl.includes("${"), false);
    assert.equal(site.logoUrl.includes("cookies"), false);
  }
});

test("site filtering matches display metadata and ignores empty queries", () => {
  const site = DEFAULT_SITES.find((item) => item.id === "acfun");
  assert.ok(site);
  assert.equal(siteMatchesQuery(site, ""), true);
  assert.equal(siteMatchesQuery(site, "AcFun"), true);
  assert.equal(siteMatchesQuery(site, "ACFUN.CN"), true);
  assert.equal(siteMatchesQuery(site, "youtube"), false);
  assert.equal(categoryLabel(site.category), "国内主流");
});

test("hostname matching handles ports, www, subdomains, and controlled aliases", () => {
  const youtube = DEFAULT_SITES.find((site) => site.id === "youtube");
  const twitter = DEFAULT_SITES.find((site) => site.id === "twitter");
  assert.equal(normalizeHostname("HTTPS://WWW.YouTube.com:443/path"), "www.youtube.com");
  assert.equal(siteMatchesHostname(youtube, "www.youtube.com"), true);
  assert.equal(siteMatchesHostname(youtube, "youtu.be"), true);
  assert.equal(siteMatchesHostname(youtube, "notyoutube.com"), false);
  assert.equal(siteMatchesHostname(twitter, "mobile.twitter.com"), true);
  assert.equal(siteMatchesHostname(twitter, "notx.com"), false);
});

test("latest probe history selects the newest valid record without mutating input", () => {
  const site = DEFAULT_SITES.find((item) => item.id === "youtube");
  const records = [
    { url: "https://youtube.com/old", ok: true, createdAt: 100 },
    { url: "not a url", ok: true, createdAt: 999 },
    { url: "https://notyoutube.com/video", ok: true, createdAt: 1000 },
    { url: "https://www.youtube.com/new", ok: false, createdAt: "2026-01-02T00:00:00Z", detail: "blocked" },
  ];
  const original = JSON.parse(JSON.stringify(records));
  assert.equal(findLatestProbeRecord(site, records), records[3]);
  assert.deepEqual(records, original);
  const restored = restoreProbeHistory([{ ...site, status: "idle", result: null }], records);
  assert.equal(restored[0].status, "fail");
  assert.equal(restored[0].result.reason, "yt-dlp 无法解析这个链接");
  assert.deepEqual(records, original);
});

test("testing status is not overwritten by asynchronous history restore", () => {
  const site = DEFAULT_SITES.find((item) => item.id === "youtube");
  const restored = restoreProbeHistory(
    [{ ...site, status: "testing", result: null }],
    [{ url: site.url, ok: true, createdAt: Date.now() }],
  );
  assert.equal(restored[0].status, "testing");
  assert.equal(restored[0].result, null);
});

test("site stats distinguish idle, testing, successful, and failed probes", () => {
  assert.deepEqual(
    deriveSiteStats([
      { status: "idle" },
      { status: "testing" },
      { status: "ok" },
      { status: "fail" },
      { status: "ok" },
    ]),
    { total: 5, ok: 2, fail: 1, idle: 1, testing: 1 },
  );
});

test("probe results are normalized to the frontend contract", () => {
  assert.deepEqual(
    normalizeProbeResult({
      ok: true,
      title: "Sample",
      duration: "12.5",
      formats: [{ format_id: "18" }],
      availableQualities: ["720p", 1080, null],
      cached: 1,
    }),
    {
      ok: true,
      title: "Sample",
      extractor: null,
      duration: 12.5,
      formatsCount: 1,
      webpageUrl: null,
      reason: null,
      detail: null,
      cached: true,
      language: null,
      availableQualities: ["720p", "1080"],
      formats: [{ format_id: "18" }],
      thumbnail: null,
      uploader: null,
    },
  );

  const failed = normalizeProbeResult({ ok: false, detail: "private video" });
  assert.equal(failed.reason, "yt-dlp 无法解析这个链接");
  assert.equal(failed.detail, "private video");
  assert.equal(failed.formatsCount, 0);
});

test("HTTP URL validation rejects unsupported and malformed values", () => {
  assert.equal(isHttpUrl("https://example.com/video"), true);
  assert.equal(isHttpUrl("http://localhost:8000/video"), true);
  assert.equal(isHttpUrl("ftp://example.com/video"), false);
  assert.equal(isHttpUrl("not a URL"), false);
  assert.equal(isHttpUrl(""), false);
});
