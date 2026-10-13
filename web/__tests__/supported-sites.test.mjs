import assert from "node:assert/strict";
import { test } from "node:test";

import { DEFAULT_SITES } from "../js/developer-sites-data.js";
import { successfulSitesFromStatus } from "../js/supported-sites.js";

test("supported site availability follows the fixed directory order", () => {
  const available = successfulSitesFromStatus({
    sites: [
      { id: "pornhub", status: "ok" },
      { id: "youtube", status: "fail" },
      { id: "vimeo", status: "ok" },
    ],
  });
  assert.deepEqual(available.map((site) => site.id), ["vimeo", "pornhub"]);
});

test("supported site availability never includes custom or unknown records", () => {
  const available = successfulSitesFromStatus({
    sites: [
      { id: "custom-1", status: "ok" },
      { id: "unknown", status: "ok" },
    ],
  });
  assert.deepEqual(available, []);
  assert.equal(DEFAULT_SITES.length, 10);
});
