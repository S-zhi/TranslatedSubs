import { test } from "node:test";
import assert from "node:assert/strict";

import { createClickGate } from "../js/developer-entry.js";
import { createDeveloperState } from "../js/developer-state.js";

test("five clicks inside the 1.5 second window trigger once", () => {
  let now = 1000;
  let triggered = 0;
  const click = createClickGate({ now: () => now, onTrigger: () => { triggered += 1; } });

  for (let i = 0; i < 5; i += 1) {
    now += 200;
    assert.equal(click(), i === 4);
  }
  assert.equal(triggered, 1);
});

test("a delayed click resets the sequence", () => {
  let now = 0;
  let triggered = 0;
  const click = createClickGate({ now: () => now, onTrigger: () => { triggered += 1; } });

  click();
  click();
  now = 1501;
  click();
  click();
  click();
  assert.equal(triggered, 0);
  click();
  click();
  assert.equal(triggered, 1);
});

test("fewer than five clicks never trigger", () => {
  let triggered = 0;
  const click = createClickGate({ now: () => 42, onTrigger: () => { triggered += 1; } });
  for (let i = 0; i < 4; i += 1) click();
  assert.equal(triggered, 0);
});

test("developer state is in memory and each page gets a fresh instance", () => {
  const first = createDeveloperState([{ level: "info", message: "one" }]);
  const second = createDeveloperState([{ level: "info", message: "one" }]);
  first.logs.length = 0;
  first.level = "error";
  assert.equal(second.logs.length, 1);
  assert.equal(second.level, "all");
});

test("developer.html includes config.js before developer.js", async () => {
  const fs = await import("node:fs/promises");
  const html = await fs.readFile(new URL("../developer.html", import.meta.url), "utf-8");
  assert.ok(html.includes('<script src="config.js"></script>'));
  assert.ok(html.indexOf('<script src="config.js"></script>') < html.indexOf('<script type="module" src="js/developer.js"></script>'));
});

