import { test } from "node:test";
import assert from "node:assert/strict";
import { initWorkbench } from "../js/workbench.js";

class EventTargetMock {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  dispatch(type, options = {}) {
    for (const callback of this.listeners.get(type) || []) callback({ type, ...options });
  }
  get listenerCount() {
    return [...this.listeners.values()].reduce((count, callbacks) => count + callbacks.size, 0);
  }
}

function setup(t, { reducedMotion = false, systemDark = false, savedMotion = null, view = "tasks" } = {}) {
  const root = { dataset: { theme: "auto" } };
  const classes = new Set();
  const host = { classList: {
    add(name) { classes.add(name); },
    remove(name) { classes.delete(name); },
    contains(name) { return classes.has(name); },
  } };
  const icon = { className: "" };
  const toggle = new EventTargetMock();
  toggle.attributes = new Map();
  toggle.setAttribute = (name, value) => toggle.attributes.set(name, value);
  toggle.querySelector = (selector) => selector === "i" ? icon : null;
  const document = new EventTargetMock();
  document.documentElement = root;
  document.visibilityState = "visible";
  document.querySelector = (selector) => ({
    "#studioVisual": host,
    "#motionToggle": toggle,
    ".view.is-active": { dataset: { view } },
  })[selector] || null;
  const reduced = new EventTargetMock();
  reduced.matches = reducedMotion;
  const dark = new EventTargetMock();
  dark.matches = systemDark;
  const window = new EventTargetMock();
  window.matchMedia = (query) => query.includes("reduced-motion") ? reduced : dark;
  const stored = new Map(savedMotion === null ? [] : [["subtrans_motion", savedMotion]]);
  const intersections = [];
  const mutations = [];
  class ObserverMock {
    constructor(callback) { this.callback = callback; this.disconnected = false; }
    observe(target, options) { this.target = target; this.options = options; }
    disconnect() { this.disconnected = true; }
  }
  const globals = {
    document,
    window,
    localStorage: {
      getItem(key) { return stored.get(key) ?? null; },
      setItem(key, value) { stored.set(key, value); },
    },
    IntersectionObserver: class extends ObserverMock {
      constructor(callback) { super(callback); intersections.push(this); }
    },
    MutationObserver: class extends ObserverMock {
      constructor(callback) { super(callback); mutations.push(this); }
    },
  };
  const original = new Map(Object.keys(globals).map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  for (const [name, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  let cleanup = () => {};
  t.after(() => {
    cleanup();
    for (const [name, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const scene = {
    motion: [], themes: [], disposals: 0,
    setMotionEnabled(value) { this.motion.push(value); },
    setTheme(value) { this.themes.push(value); },
    dispose() { this.disposals += 1; },
  };
  const creations = [];
  const module = {
    createWorkbenchScene(target, options) { creations.push({ target, options }); return scene; },
  };
  return {
    root, host, toggle, document, window, reduced, dark, stored, intersections, mutations, scene, creations, module,
    start(loadScene = () => module) { cleanup = initWorkbench({ loadScene }); return cleanup; },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("reduced motion keeps a static scene and follows preference changes", async (t) => {
  const ui = setup(t, { reducedMotion: true });
  ui.start();
  await settle();
  assert.equal(ui.creations.length, 1);
  assert.deepEqual(ui.creations[0], { target: ui.host, options: { reducedMotion: false } });
  assert.equal(ui.scene.motion.at(-1), false);
  assert.equal(ui.root.dataset.motion, "off");
  assert.equal(ui.toggle.disabled, true);
  assert.equal(ui.toggle.attributes.get("aria-pressed"), "false");

  ui.reduced.matches = false;
  ui.reduced.dispatch("change");
  assert.equal(ui.scene.motion.at(-1), true);
  assert.equal(ui.root.dataset.motion, "on");
  assert.equal(ui.toggle.disabled, false);
  ui.reduced.matches = true;
  ui.reduced.dispatch("change");
  assert.equal(ui.scene.motion.at(-1), false);
});

test("saved manual pause and toggle changes persist without overriding reduced motion", async (t) => {
  const ui = setup(t, { savedMotion: "off" });
  ui.start();
  await settle();
  assert.equal(ui.scene.motion.at(-1), false);
  assert.equal(ui.toggle.attributes.get("aria-pressed"), "false");
  ui.toggle.dispatch("click");
  assert.equal(ui.scene.motion.at(-1), true);
  assert.equal(ui.stored.get("subtrans_motion"), "on");
  ui.toggle.dispatch("click");
  assert.equal(ui.scene.motion.at(-1), false);
  assert.equal(ui.stored.get("subtrans_motion"), "off");

  ui.reduced.matches = true;
  ui.reduced.dispatch("change");
  ui.reduced.matches = false;
  ui.reduced.dispatch("change");
  assert.equal(ui.scene.motion.at(-1), false);
  assert.equal(ui.root.dataset.motion, "off");
});

test("scene loads only in tasks and pauses for hidden views, viewport, and cached pages", async (t) => {
  const ui = setup(t, { view: "other-settings" });
  let loads = 0;
  ui.start(() => { loads += 1; return ui.module; });
  await settle();
  assert.equal(loads, 0);
  ui.document.dispatch("viewchange", { detail: { view: "tasks" } });
  await settle();
  assert.equal(loads, 1);
  assert.equal(ui.scene.motion.at(-1), true);
  const updates = ui.scene.motion.length;
  ui.document.dispatch("viewchange", { detail: { view: "tasks" } });
  assert.equal(ui.scene.motion.length, updates, "task updates must not restart motion");
  ui.document.dispatch("viewchange", { detail: { view: "model-settings" } });
  assert.equal(ui.scene.motion.at(-1), false);
  ui.document.dispatch("viewchange", { detail: { view: "tasks" } });
  assert.equal(ui.scene.motion.at(-1), true);

  ui.document.visibilityState = "hidden";
  ui.document.dispatch("visibilitychange");
  assert.equal(ui.scene.motion.at(-1), false);
  ui.document.visibilityState = "visible";
  ui.document.dispatch("visibilitychange");
  assert.equal(ui.scene.motion.at(-1), true);
  ui.intersections[0].callback([{ isIntersecting: false }]);
  assert.equal(ui.scene.motion.at(-1), false);
  ui.intersections[0].callback([{ isIntersecting: true }]);
  assert.equal(ui.scene.motion.at(-1), true);
  ui.window.dispatch("pagehide", { persisted: true });
  assert.equal(ui.scene.motion.at(-1), false);
  assert.equal(ui.scene.disposals, 0);
  ui.window.dispatch("pageshow", { persisted: true });
  assert.equal(ui.scene.motion.at(-1), true);
  assert.equal(loads, 1);
});

test("theme changes update the scene and final page exit disposes all listeners once", async (t) => {
  const ui = setup(t, { systemDark: true });
  const cleanup = ui.start();
  await settle();
  assert.equal(ui.scene.themes.at(-1), true);
  assert.deepEqual(ui.mutations[0].options.attributeFilter, ["data-theme"]);
  ui.root.dataset.theme = "light";
  ui.mutations[0].callback([{ attributeName: "data-theme" }]);
  assert.equal(ui.scene.themes.at(-1), false);
  ui.root.dataset.theme = "auto";
  ui.dark.matches = false;
  ui.dark.dispatch("change");
  assert.equal(ui.scene.themes.at(-1), false);

  ui.window.dispatch("pagehide", { persisted: false });
  cleanup();
  assert.equal(ui.scene.disposals, 1);
  assert.equal(ui.intersections[0].disconnected, true);
  assert.equal(ui.mutations[0].disconnected, true);
  for (const target of [ui.toggle, ui.reduced, ui.dark, ui.document, ui.window]) {
    assert.equal(target.listenerCount, 0);
  }
  const updates = ui.scene.motion.length;
  ui.toggle.dispatch("click");
  ui.document.dispatch("viewchange", { detail: { view: "tasks" } });
  ui.window.dispatch("pageshow");
  assert.equal(ui.scene.motion.length, updates);
});

test("optional scene load failure stays contained and preserves the fallback", async (t) => {
  const ui = setup(t);
  ui.host.classList.add("is-ready");
  let loads = 0;
  assert.doesNotThrow(() => ui.start(async () => { loads += 1; throw new Error("renderer unavailable"); }));
  await settle();
  assert.equal(ui.host.classList.contains("scene-unavailable"), true);
  assert.equal(ui.host.classList.contains("is-ready"), false);
  assert.equal(ui.creations.length, 0);
  ui.toggle.dispatch("click");
  ui.document.dispatch("viewchange", { detail: { view: "other-settings" } });
  ui.document.dispatch("viewchange", { detail: { view: "tasks" } });
  await settle();
  assert.equal(loads, 1, "failed decoration must not retry on every UI update");
});

test("cleanup during an asynchronous import prevents a late scene from being created", async (t) => {
  const ui = setup(t);
  let resolveScene;
  let loads = 0;
  const pending = new Promise((resolve) => { resolveScene = resolve; });
  const cleanup = ui.start(() => { loads += 1; return pending; });
  await settle();
  assert.equal(loads, 1);
  cleanup();
  cleanup();
  resolveScene(ui.module);
  await settle();
  assert.equal(ui.creations.length, 0);
  assert.equal(ui.scene.disposals, 0, "no controller exists to dispose");
  assert.equal(ui.intersections[0].disconnected, true);
  assert.equal(ui.mutations[0].disconnected, true);
  for (const target of [ui.toggle, ui.reduced, ui.dark, ui.document, ui.window]) {
    assert.equal(target.listenerCount, 0);
  }
});
