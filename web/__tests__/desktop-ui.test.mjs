import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { mountDesktop, applyDesktopDefaults } from '../js/desktop-ui.js';
import { createDesktopController } from '../js/desktop.js';
import { exportDesktopArtifact, openDesktopUpdate, verifiedHttpsUrl, bindDesktopQueue } from '../js/desktop-bridge.js';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = new Map(); this.dataset = {};
    this.attributes = {}; this.hidden = false; this.disabled = false; this.value = '';
    const classes = new Set();
    this.classList = { add: (v) => classes.add(v), remove: (v) => classes.delete(v),
      contains: (v) => classes.has(v), toggle: (v, on) => on ? classes.add(v) : classes.delete(v) };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn, capture) { this.listeners.set(name, { fn, capture }); }
  removeEventListener(name) { this.listeners.delete(name); }
  async dispatch(name, event = {}) { return this.listeners.get(name)?.fn(event); }
  focus() {}
}
const defaults = { sourceLang: 'en', targetLang: 'zh-CN', mode: 'bilingual', burn: 'hard',
  model: 'local:base.en', engine: 'local-opus-en-zh', needSubtitle: true, ttsEnabled: false };
function fakeWindow(enabled = true) {
  const win = new Element();
  win.SUBTRANS_DESKTOP = { enabled }; win.messages = [];
  win.webkit = { messageHandlers: { desktop: { postMessage: (value) => win.messages.push(value) } } };
  return win;
}
function fakeController() {
  const listeners = new Set();
  return {
    snapshot: { ready: false, defaults, models: { state: 'preparing', models: [] } },
    starts: 0, stops: 0, retries: 0,
    subscribe(fn) { listeners.add(fn); fn(this.snapshot); return () => listeners.delete(fn); },
    emit(patch) { this.snapshot = { ...this.snapshot, ...patch }; listeners.forEach((fn) => fn(this.snapshot)); },
    async start() { this.starts++; }, stop() { this.stops++; }, async retry() { this.retries++; },
    async refresh() { return this.snapshot; },
  };
}
function fakeDocument() {
  const doc = new Element();
  const nodes = new Map(['#desktopStatus', '#desktopUpdate'].map((key) => [key, new Element()]));
  doc.body = new Element(); doc.createElement = (tag) => new Element(tag);
  doc.querySelector = (key) => nodes.get(key) || null;
  return { doc, nodes };
}

test('native exports use real task IDs and fixed kinds; browser fallback is left untouched', () => {
  const win = fakeWindow();
  assert.equal(exportDesktopArtifact('task_a-123', 'video', win), true);
  assert.equal(exportDesktopArtifact('task_b', 'subtitle', win), true);
  assert.deepEqual(win.messages, [{ action: 'export', taskId: 'task_a-123', kind: 'video' },
    { action: 'export', taskId: 'task_b', kind: 'subtitle' }]);
  for (const [id, kind] of [['../secret', 'video'], ['task', 'source'], ['task', 'dubbed'], ['a'.repeat(129), 'video']]) {
    assert.equal(exportDesktopArtifact(id, kind, win), false);
  }
  assert.equal(exportDesktopArtifact('task', 'video', fakeWindow(false)), false);
  assert.equal(exportDesktopArtifact('task', 'video', { SUBTRANS_DESKTOP: { enabled: true } }), false);
});

test('update navigation accepts credential-free HTTPS only', () => {
  const win = fakeWindow();
  for (const url of ['http://example.org/app.dmg', 'javascript:alert(1)', 'file:///etc/passwd', '/relative',
    'https://user:password@example.org/app.dmg', `https://example.org/${'a'.repeat(2100)}`]) {
    assert.equal(verifiedHttpsUrl(url), null);
    assert.equal(openDesktopUpdate(url, win), false);
  }
  assert.equal(openDesktopUpdate('https://example.org/app.dmg', win), true);
  assert.deepEqual(win.messages, [{ action: 'openExternal', url: 'https://example.org/app.dmg' }]);
});

test('desktop mount renders phases and errors safely, retries, and cancels/restarts page lifecycle', async () => {
  const { doc, nodes } = fakeDocument();
  const win = fakeWindow(); const controller = fakeController();
  const view = mountDesktop({ documentLike: doc, windowLike: win, controller });
  assert.equal(doc.body.classList.contains('desktop-mode'), true);
  const host = nodes.get('#desktopStatus');
  const [heading, message, rows, retry] = host.children;
  controller.emit({ models: { state: 'preparing', models: [{ name: '<script>unsafe</script>', status: 'downloading',
    downloaded_bytes: 512, total_bytes: null }] } });
  assert.equal(rows.children[0].children[0].textContent, '<script>unsafe</script>');
  const progress = rows.children[0].children.find((node) => node.tagName === 'progress');
  assert.equal(progress.value, ''); // no percentage for unknown total
  controller.emit({ error: '空间不足', models: { state: 'failed', models: [] } });
  assert.match(heading.textContent, /需要处理/);
  assert.equal(message.textContent, '空间不足');
  assert.equal(retry.hidden, false);
  await retry.dispatch('click');
  assert.equal(controller.retries, 1);
  controller.emit({ ready: true, error: null, models: { state: 'ready', models: [] } });
  assert.equal(retry.hidden, true);
  assert.match(heading.textContent, /已就绪/);
  view.start(); await win.dispatch('pagehide'); await win.dispatch('pageshow');
  assert.equal(controller.starts, 2); assert.equal(controller.stops, 1);
  view.dispose();
  assert.equal(win.listeners.has('pagehide'), false);
  assert.equal(win.listeners.has('pageshow'), false);
});

test('update banner renders version/notes as text and never changes task readiness', async () => {
  const { doc, nodes } = fakeDocument(); const win = fakeWindow(); const controller = fakeController();
  const view = mountDesktop({ documentLike: doc, windowLike: win, controller });
  controller.emit({ ready: true, updates: { state: 'available', current_version: '1.0.0', latest_version: '1.1.0',
    release_notes: '<b>Changes</b>\nFixed subtitles', download_url: 'https://example.org/app.dmg' } });
  const [title, notes, download] = nodes.get('#desktopUpdate').children;
  assert.match(title.textContent, /1.1.0/);
  assert.equal(notes.textContent, '<b>Changes</b>\nFixed subtitles');
  assert.equal(download.hidden, false);
  await download.dispatch('click');
  assert.equal(win.messages[0].action, 'openExternal');
  controller.emit({ updates: { state: 'disabled' } });
  assert.equal(download.hidden, true);
  assert.match(title.textContent, /尚未配置/);
  assert.equal(controller.snapshot.ready, true);
  view.dispose();
});

test('failed updates offer a single retry, checking hides download, and current is clearly complete', async () => {
  const { doc, nodes } = fakeDocument(); const controller = fakeController();
  let complete; let checks = 0;
  controller.checkUpdates = ({ force }) => {
    assert.equal(force, true); checks++;
    controller.emit({ updates: { state: 'checking' } });
    return new Promise((resolve) => { complete = resolve; });
  };
  const view = mountDesktop({ documentLike: doc, windowLike: fakeWindow(), controller });
  const [title, notes, download, retry] = nodes.get('#desktopUpdate').children;
  controller.emit({ ready: true, updates: { state: 'failed', error: 'Internal transport error' } });
  assert.equal(retry.hidden, false);
  assert.match(notes.textContent, /稍后自动检查/);
  assert.doesNotMatch(notes.textContent, /Internal|transport/);
  const retrying = retry.dispatch('click');
  await retry.dispatch('click');
  assert.equal(checks, 1);
  assert.equal(retry.disabled, true);
  assert.equal(retry.hidden, true);
  assert.equal(download.hidden, true);
  assert.equal(title.textContent, '正在检查更新');
  assert.equal(controller.snapshot.ready, true);
  controller.emit({ updates: { state: 'current', current_version: '1.0.0' } });
  complete(); await retrying;
  assert.equal(title.textContent, '已是最新版本 1.0.0');
  assert.equal(retry.disabled, false);
  assert.equal(retry.hidden, true);
  assert.equal(notes.textContent, '');
  view.dispose();
});

test('mount is a no-op for server mode', () => {
  const { doc } = fakeDocument();
  assert.equal(mountDesktop({ documentLike: doc, windowLike: fakeWindow(false), controller: fakeController() }), null);
  assert.equal(doc.body.classList.contains('desktop-mode'), false);
});

test('queue capture exports by row ID, blocks retry before ready, and preserves browser handlers', async () => {
  const doc = new Element(); const win = fakeWindow(); const controller = fakeController();
  const notices = []; const cleanup = bindDesktopQueue({ documentLike: doc, windowLike: win, controller, notify: (v) => notices.push(v) });
  assert.equal(doc.listeners.get('click').capture, true);
  const click = async (icon) => {
    const event = { prevented: false, stopped: false, preventDefault() { this.prevented = true; },
      stopImmediatePropagation() { this.stopped = true; }, target: { closest: () => ({
        closest: () => ({ dataset: { id: 'actual-task-id' } }), querySelector: (selector) => selector === icon,
      }) } };
    await doc.dispatch('click', event); return event;
  };
  assert.equal((await click('.ph-download-simple')).stopped, true);
  assert.deepEqual(win.messages[0], { action: 'export', taskId: 'actual-task-id', kind: 'video' });
  assert.equal((await click('.ph-arrow-clockwise')).stopped, true);
  assert.equal(notices.length, 1);
  controller.emit({ ready: true });
  assert.equal((await click('.ph-arrow-clockwise')).stopped, false);
  delete win.webkit;
  assert.equal((await click('.ph-download-simple')).stopped, false);
  cleanup(); assert.equal(doc.listeners.size, 0);
});

function consoleDocument() {
  const { doc, nodes } = fakeDocument();
  for (const id of ['taskForm', 'sourceLang', 'targetLang', 'model', 'engine', 'url', 'videoFile', 'uploadBtn',
    'urlHint', 'consoleBar', 'submitBtn', 'ttsHint', 'quality']) nodes.set(`#${id}`, new Element());
  const form = nodes.get('#taskForm');
  form.elements = {};
  for (const id of ['sourceLang', 'targetLang', 'model', 'engine', 'quality']) form.elements[id] = nodes.get(`#${id}`);
  for (const key of ['ttsEnabled', 'ttsVoice', 'originalVoiceMode']) form.elements[key] = new Element();
  form.elements.ttsVoice.value = 'auto'; form.elements.originalVoiceMode.value = 'keep';
  const groups = { burn: ['soft', 'hard'], mode: ['mono', 'bilingual'], needSubtitle: ['off', 'on'] };
  const radios = {};
  for (const [key, values] of Object.entries(groups)) {
    radios[key] = values.map((value) => Object.assign(new Element('input'), { value, checked: false }));
    form.elements[key] = { get value() { return radios[key].find((input) => input.checked)?.value || ''; } };
  }
  form.querySelectorAll = (selector) => radios[selector.match(/name="(.*?)"/)?.[1]] || [];
  form.querySelector = () => new Element();
  return { doc, nodes, form };
}

// Exercise the real console with the real store dispatch and injected APIs.
const win = fakeWindow();
win.APP_CONFIG = { USE_MOCK: false, API_BASE_URL: 'http://127.0.0.1:8000' };
globalThis.window = win;
const { Api } = await import('../js/api.js');
const { initConsole } = await import('../js/ui-console.js');

test('console blocks not-ready submissions, avoids legacy settings APIs, then submits local upload and YouTube', async () => {
  const { doc, nodes, form } = consoleDocument(); globalThis.document = doc;
  const controller = fakeController();
  const calls = []; let unexpected = 0;
  const originals = new Map();
  const stub = (name, fn) => { originals.set(name, Api[name]); Api[name] = fn; };
  for (const name of ['listTranslationEngines', 'getModelSettings', 'getRuntimeReadiness', 'listVideoLanguages', 'listTargetLanguages']) {
    stub(name, async () => { unexpected++; throw new Error(`Unexpected legacy API: ${name}`); });
  }
  stub('createUploadTask', async (payload) => { calls.push(['upload', payload]); return { id: 'upload-id', status: 'SUCCESS' }; });
  stub('createTask', async (payload) => { calls.push(['url', payload]); return { id: 'url-id', status: 'SUCCESS' }; });
  stub('probeVideo', async () => ({ ok: true }));
  try {
    initConsole({ desktop: controller });
    assert.equal(nodes.get('#submitBtn').disabled, true);
    const submit = () => form.dispatch('submit', { preventDefault() {} });
    await submit();
    assert.equal(calls.length, 0);
    const file = { name: 'lecture.mp4', type: 'video/mp4' };
    nodes.get('#videoFile').files = [file];
    controller.emit({ ready: true });
    assert.equal(nodes.get('#submitBtn').disabled, false);
    assert.equal(form.elements.mode.value, 'bilingual');
    assert.equal(nodes.get('#sourceLang').disabled, true);
    await submit();
    assert.equal(calls[0][0], 'upload');
    assert.equal(calls[0][1].file, file);
    assert.equal(calls[0][1].quality, '1080p');
    for (const [key, value] of Object.entries(defaults)) assert.equal(calls[0][1][key], value);
    nodes.get('#videoFile').files = [];
    nodes.get('#url').value = 'https://youtube.com/watch?v=example';
    await submit();
    assert.equal(calls[1][0], 'url');
    assert.equal(calls[1][1].url, 'https://youtube.com/watch?v=example');
    assert.equal(unexpected, 0);
  } finally { for (const [name, value] of originals) Api[name] = value; }
});

test('real controller inventory validation preserves disabled desktop defaults through task submission', async () => {
  const { doc, nodes, form } = consoleDocument(); globalThis.document = doc;
  const timers = new Map(); let nextId = 0; let inventoryReady = true;
  const selectedDefaults = { ...defaults, model: 'local:small.en' };
  const controller = createDesktopController({
    setTimer(fn, delay) { timers.set(++nextId, { fn, delay }); return nextId; },
    clearTimer(id) { timers.delete(id); },
    fetchImpl: async (path) => ({ ok: true, json: async () => path.includes('updates')
      ? { state: 'disabled' }
      : path.includes('local-models') ? [
        { name: 'base.en', status: 'READY' },
        { name: 'small.en', status: inventoryReady ? 'READY' : 'FAILED' },
      ] : { ready: true, defaults: selectedDefaults, models: { state: 'ready', ready: true, models: [] } } }),
  });
  const original = Api.createUploadTask; const submitted = [];
  Api.createUploadTask = async (payload) => { submitted.push(payload); return { id: 'desktop-defaults', status: 'SUCCESS' }; };
  try {
    initConsole({ desktop: controller });
    await controller.start();
    for (const key of ['sourceLang', 'targetLang', 'model', 'engine']) {
      assert.equal(form.elements[key].value, selectedDefaults[key]);
      assert.equal(form.elements[key].disabled, true);
    }
    assert.equal(form.elements.model.children[0].textContent, '本地英语转写');
    for (const key of ['mode', 'burn', 'needSubtitle']) {
      assert.ok(form.querySelectorAll(`input[name="${key}"]`).every((input) => input.disabled));
      assert.equal(form.elements[key].value, key === 'needSubtitle' ? 'on' : selectedDefaults[key]);
    }
    assert.equal(form.elements.ttsEnabled.disabled, true);
    assert.equal(form.elements.ttsEnabled.checked, false);
    nodes.get('#videoFile').files = [{ name: 'lecture.mp4', type: 'video/mp4' }];
    await form.dispatch('submit', { preventDefault() {} });
    assert.equal(submitted.length, 1);
    for (const [key, value] of Object.entries(selectedDefaults)) assert.equal(submitted[0][key], value);
    inventoryReady = false;
    await controller.refresh();
    assert.equal(nodes.get('#submitBtn').disabled, true);
    assert.equal(form.elements.model.value, selectedDefaults.model);
    assert.match(controller.snapshot.error, /库存/);
    inventoryReady = true;
    await controller.refresh();
    assert.equal(nodes.get('#submitBtn').disabled, false);
    assert.equal(form.elements.model.value, selectedDefaults.model);
  } finally { controller.stop(); Api.createUploadTask = original; }
  assert.equal(timers.size, 0);
});

test('a readiness change during URL probing prevents task creation', async () => {
  const { doc, nodes, form } = consoleDocument(); globalThis.document = doc;
  const controller = fakeController(); controller.emit({ ready: true });
  const oldProbe = Api.probeVideo; const oldCreate = Api.createTask;
  let created = false;
  Api.probeVideo = async () => { controller.emit({ ready: false }); return { ok: true }; };
  Api.createTask = async () => { created = true; };
  try {
    initConsole({ desktop: controller });
    nodes.get('#url').value = 'https://youtube.com/watch?v=example';
    await form.dispatch('submit', { preventDefault() {} });
    assert.equal(created, false);
    assert.equal(nodes.get('#submitBtn').disabled, true);
  } finally { Api.probeVideo = oldProbe; Api.createTask = oldCreate; }
});

test('duplicate submits during readiness refresh share the locked form', async () => {
  const { doc, nodes, form } = consoleDocument(); globalThis.document = doc;
  const controller = fakeController(); controller.emit({ ready: true });
  let resolve; let refreshes = 0;
  controller.refresh = () => { refreshes++; return new Promise((done) => { resolve = done; }); };
  initConsole({ desktop: controller });
  const first = form.dispatch('submit', { preventDefault() {} });
  await form.dispatch('submit', { preventDefault() {} });
  assert.equal(refreshes, 1);
  assert.equal(nodes.get('#submitBtn').disabled, true);
  controller.emit({ ready: false }); resolve(); await first;
});

test('main desktop boot skips legacy settings and Drive initialization, while server boot retains them', async () => {
  const source = await readFile(new URL('../js/main.js', import.meta.url), 'utf8');
  const names = [...source.matchAll(/import \{([^}]+)\} from [^;]+;/g)]
    .flatMap((match) => match[1].split(',').map((name) => name.trim()));
  const executable = source.replace(/import \{[^}]+\} from [^;]+;/g, '');
  for (const desktop of [true, false]) {
    const calls = []; const { doc } = fakeDocument(); const win = fakeWindow(desktop);
    const context = Object.fromEntries(names.map((name) => [name, () => calls.push(name)]));
    context.isDesktop = () => desktop;
    context.mountDesktop = () => ({ controller: {}, start: () => calls.push('desktop.start') });
    context.document = doc; context.window = win;
    context.initRouter = () => { doc.body.dataset.route = 'tasks'; };
    vm.runInNewContext(executable, context);
    for (const name of ['initAdvancedSettings', 'initModelSettings', 'initDrive', 'initTranslationSettings',
      'initAudioSettings', 'initLocalModels', 'initProbe', 'initSupportedSitesView']) {
      assert.equal(calls.includes(name), !desktop, name);
    }
    assert.equal(calls.includes('desktop.start'), desktop);
    assert.equal(calls.includes('loadTasks'), true);
  }
});
