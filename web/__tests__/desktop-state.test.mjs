import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDesktopController, desktopDefaults, desktopTaskPayload, isDesktop } from '../js/desktop.js';
import { desktopModelProgress, formatDesktopBytes } from '../js/desktop-ui.js';

const defaults = { sourceLang: 'en', targetLang: 'zh-CN', mode: 'bilingual', burn: 'hard',
  model: 'local:base.en', engine: 'local-opus-en-zh', needSubtitle: true, ttsEnabled: false };
const status = (state = 'ready') => ({ ready: state === 'ready', version: '1.0.0', defaults,
  models: { state, ready: state === 'ready', models: [] } });
const response = (body) => ({ ok: true, json: async () => body });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function fixture(handler, options = {}) {
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  const controller = createDesktopController({
    setTimer(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimer(id) { timers.delete(id); },
    fetchImpl: async (path, opts) => {
      requests.push({ path, ...opts });
      if (path.startsWith('/api/desktop/updates')) return response({ state: 'disabled' });
      if (path === '/api/srt/local-models') return response([{ name: 'base.en', status: 'READY' }]);
      return response(await handler(path, opts));
    }, ...options,
  });
  return { controller, requests, timers };
}

test('desktop is explicitly enabled, defaults are backend supplied and whitelisted', () => {
  assert.equal(isDesktop({ SUBTRANS_DESKTOP: { enabled: true } }), true);
  assert.equal(isDesktop({ SUBTRANS_DESKTOP: { enabled: 'true' } }), false);
  assert.equal(isDesktop({}), false);
  assert.deepEqual(desktopDefaults({ ...defaults, apiKey: 'must-not-copy' }), defaults);
  assert.equal(desktopDefaults({ ...defaults, model: 'remote:base' }), null);
  assert.equal(desktopDefaults({ ...defaults, ttsEnabled: true }), null);
  assert.equal(desktopDefaults({ ...defaults, sourceLang: 'auto' }), null);
});

test('automatic prepare is followed by status polling and inventory validation', async () => {
  let current = status('not_started');
  const { controller, requests, timers } = fixture((path) => {
    if (path.endsWith('/prepare')) { current = status('preparing'); return current.models; }
    return current;
  });
  await controller.start();
  assert.equal(controller.snapshot.ready, false);
  assert.deepEqual(requests.filter((r) => r.method === 'POST').map((r) => r.path), ['/api/desktop/models/prepare']);
  assert.equal(timers.size, 1);
  current = status();
  await controller.refresh();
  assert.equal(controller.snapshot.ready, true);
  assert.equal(requests.at(-1).path, '/api/srt/local-models');
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 10000);
  for (const request of requests) {
    assert.equal(request.credentials, 'same-origin');
    assert.equal(request.headers, undefined);
    assert.ok(request.path.startsWith('/api/'));
  }
  controller.stop();
  assert.equal(timers.size, 0);
});

test('failed prepare remains failed until an explicit retry', async () => {
  let current = status('failed');
  current.models.error = '磁盘空间不足';
  const { controller, requests } = fixture((path) => {
    if (path.endsWith('/retry')) { current = status('preparing'); return current.models; }
    return current;
  });
  await controller.start();
  await controller.refresh();
  assert.equal(controller.snapshot.error, '磁盘空间不足');
  assert.equal(requests.filter((r) => r.method === 'POST').length, 0);
  await controller.retry();
  assert.equal(controller.snapshot.models.state, 'preparing');
  assert.equal(controller.snapshot.error, null);
  assert.equal(requests.filter((r) => r.path.endsWith('/retry')).length, 1);
  controller.stop();
});

test('ready requires valid defaults, backend dependency readiness and local inventory', async () => {
  for (const current of [
    { ...status(), ready: false },
    { ...status(), defaults: undefined },
    { ...status(), defaults: { ...defaults, mode: 'mono' } },
  ]) {
    const { controller } = fixture(() => current);
    await controller.start();
    assert.equal(controller.snapshot.ready, false);
    controller.stop();
  }
  const { controller } = fixture(() => status(), { fetchImpl: async (path) => response(
    path.includes('updates') ? { state: 'current' } : path.includes('local-models') ? [] : status()) });
  await controller.start();
  assert.equal(controller.snapshot.ready, false);
  assert.match(controller.snapshot.error, /库存/);
  controller.stop();
});

test('network loss revokes readiness, then restart/not_started triggers prepare again', async () => {
  let current = status();
  const { controller, requests } = fixture((path) => {
    if (current instanceof Error) throw current;
    return path.endsWith('/prepare') ? status('preparing').models : current;
  });
  await controller.start();
  assert.equal(controller.snapshot.ready, true);
  current = new Error('offline');
  await controller.refresh();
  assert.equal(controller.snapshot.ready, false);
  current = status('not_started');
  await controller.refresh();
  assert.equal(requests.at(-1).path, '/api/desktop/models/prepare');
  controller.stop();
});

test('overlapping polls share one request and late results cannot revive a stopped lifecycle', async () => {
  let resolve;
  const { controller, requests, timers } = fixture(() => new Promise((done) => { resolve = done; }));
  const start = controller.start();
  assert.equal(controller.refresh(), start);
  assert.equal(requests.filter((r) => r.path.endsWith('/status')).length, 1);
  controller.stop();
  assert.equal(requests.find((r) => r.path.endsWith('/status')).signal.aborted, true);
  resolve(status());
  await start;
  assert.equal(controller.snapshot.ready, false);
  assert.equal(timers.size, 0);
});

test('new lifecycle ignores stale old status even when old fetch does not honor abort', async () => {
  const pending = [];
  const { controller } = fixture(() => new Promise((done) => pending.push(done)));
  const first = controller.start();
  controller.stop();
  const second = controller.start();
  pending[1](status('preparing'));
  await second;
  pending[0](status());
  await first;
  assert.equal(controller.snapshot.models.state, 'preparing');
  assert.equal(controller.snapshot.ready, false);
  controller.stop();
});

test('updates failure does not gate ready models', async () => {
  const { controller } = fixture(() => status(), { fetchImpl: async (path) => {
    if (path.includes('updates')) throw new Error('offline updates');
    return response(path.includes('local-models') ? [{ name: 'base.en', status: 'READY' }] : status());
  } });
  await controller.start();
  await flush();
  assert.equal(controller.snapshot.ready, true);
  assert.equal(controller.snapshot.updates.state, 'failed');
  controller.stop();
});

test('hung requests time out and release polling for recovery', async () => {
  const { controller, timers } = fixture(() => status(), { fetchImpl: (path, { signal }) =>
    path.includes('updates') ? Promise.resolve(response({ state: 'disabled' })) : new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }) });
  const pending = controller.start();
  await flush();
  [...timers.values()].find((timer) => timer.delay === 15000).fn();
  await pending;
  assert.match(controller.snapshot.error, /超时/);
  assert.equal(controller.snapshot.ready, false);
  assert.ok([...timers.values()].some((timer) => timer.delay === 1500));
  controller.stop();
});

test('unknown sizes and conversion never invent percentages; download bytes are retained', () => {
  assert.equal(formatDesktopBytes(null), '大小未知');
  assert.equal(formatDesktopBytes(NaN), '大小未知');
  assert.equal(formatDesktopBytes(0), '0 B');
  const model = { status: 'downloading', downloaded_bytes: 512, total_bytes: null };
  assert.deepEqual(desktopModelProgress(model), { phase: '下载中', size: '512 B / 大小未知', percent: null });
  assert.equal(desktopModelProgress({ ...model, total_bytes: 1024 }).percent, 50);
  assert.equal(desktopModelProgress({ ...model, status: 'converting', total_bytes: 1024 }).percent, null);
  assert.equal(desktopModelProgress({ ...model, phase: 'checking', total_bytes: 1024 }).percent, null);
});

test('task payload preserves existing upload/URL APIs but overrides stale cloud/TTS fields', () => {
  const file = { name: 'lecture.mp4' };
  const result = desktopTaskPayload({ file, engine: 'cloud', sourceLang: 'ja', ttsEnabled: true, quality: '1080p' },
    { ready: true, defaults });
  assert.equal(result.file, file);
  assert.equal(result.quality, '1080p');
  for (const [key, value] of Object.entries(defaults)) assert.equal(result[key], value);
  assert.equal(desktopTaskPayload({ url: 'https://youtube.com/watch?v=example' }, { ready: true, defaults }).url,
    'https://youtube.com/watch?v=example');
  assert.throws(() => desktopTaskPayload({}, { ready: false, defaults }), /尚未就绪/);
});
