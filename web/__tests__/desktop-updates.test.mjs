import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDesktopController } from '../js/desktop.js';

const defaults = { sourceLang: 'en', targetLang: 'zh-CN', mode: 'bilingual', burn: 'hard',
  model: 'local:base.en', engine: 'local-opus-en-zh', needSubtitle: true, ttsEnabled: false };
const ready = { ready: true, defaults, models: { state: 'ready', ready: true, models: [] } };
const result = (state, fields = {}) => ({ state, current_version: '1.0.0', latest_version: null,
  release_notes: null, download_url: null, error: null, ...fields });
const response = (body) => ({ ok: true, json: async () => body });
const flush = () => new Promise((resolve) => setImmediate(resolve));

// Model UpdateService.check's actual snapshot contract: a non-forced GET starts
// a worker, returns checking, and only a later GET sees its cached completion.
function setup({ disabled = false, updateFetch } = {}) {
  const timers = new Map(); const calls = [];
  let nextId = 0; let now = 0; let expires = 0; let running = false; let launches = 0;
  let cached = result(disabled ? 'disabled' : 'checking');
  const controller = createDesktopController({
    updatePollMs: 2000,
    setTimer(fn, delay) { timers.set(++nextId, { fn, delay }); return nextId; },
    clearTimer(id) { timers.delete(id); },
    fetchImpl: async (path, options) => {
      if (!path.includes('/updates')) return response(path.includes('local-models')
        ? [{ name: 'base.en', status: 'READY' }] : ready);
      calls.push({ path, ...options });
      if (updateFetch) return updateFetch(path, options);
      if (disabled || running || (!path.endsWith('force=true') && now < expires)) return response(cached);
      running = true; launches++;
      cached = result('checking');
      return response(cached);
    },
  });
  return {
    controller, timers, calls,
    get launches() { return launches; },
    finish(value) { cached = value; running = false; expires = now + 3600000; },
    async tick(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected timer with delay ${delay}`);
      timers.delete(entry[0]); now += delay; entry[1].fn(); await flush();
    },
  };
}

for (const state of ['available', 'current', 'failed']) {
  test(`asynchronous checking reaches ${state} and rechecks after the cache interval`, async () => {
    const fixture = setup(); const { controller, timers, calls } = fixture;
    try {
      await controller.start(); await flush();
      assert.equal(controller.snapshot.updates.state, 'checking');
      assert.equal(controller.snapshot.ready, true);
      await fixture.tick(2000);
      assert.equal(fixture.launches, 1); // polling does not force a new worker
      const terminal = result(state, state === 'available' ? { latest_version: '1.1.0',
        release_notes: '字幕修复', download_url: 'https://example.org/app.dmg' }
        : state === 'failed' ? { error: 'Update check timed out; retry later.' } : {});
      fixture.finish(terminal);
      await fixture.tick(2000);
      assert.deepEqual(controller.snapshot.updates, terminal);
      assert.equal(controller.snapshot.ready, true);
      assert.equal([...timers.values()].some((timer) => timer.delay === 2000), false);
      await fixture.tick(3600000);
      assert.equal(controller.snapshot.updates.state, 'checking');
      assert.equal(fixture.launches, 2);
      fixture.finish(result('current'));
      await fixture.tick(2000);
      assert.equal(controller.snapshot.updates.state, 'current');
      assert.ok(calls.every((call) => call.path.endsWith('force=false')));
    } finally { controller.stop(); }
    assert.equal(timers.size, 0);
  });
}

test('disabled channel is terminal without repeated update requests', async () => {
  const { controller, calls, timers } = setup({ disabled: true });
  await controller.start(); await flush();
  assert.equal(controller.snapshot.updates.state, 'disabled');
  await controller.refresh();
  assert.equal(calls.length, 1);
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [10000]);
  controller.stop();
});

test('explicit retry bypasses cached failure once, then polls without force', async () => {
  const fixture = setup(); const { controller, calls } = fixture;
  try {
    await controller.start(); await flush();
    fixture.finish(result('failed', { error: 'offline' }));
    await fixture.tick(2000);
    const retry = controller.checkUpdates({ force: true });
    assert.equal(controller.checkUpdates({ force: true }), retry);
    await retry;
    assert.equal(controller.snapshot.updates.state, 'checking');
    assert.equal(fixture.launches, 2);
    fixture.finish(result('available', { latest_version: '1.2.0', download_url: 'https://example.org/app.dmg' }));
    await fixture.tick(2000);
    assert.equal(controller.snapshot.updates.state, 'available');
    assert.equal(calls.filter((call) => call.path.endsWith('force=true')).length, 1);
    assert.ok(calls.at(-1).path.endsWith('force=false'));
  } finally { controller.stop(); }
});

test('update timeout leaves model refresh usable and schedules recovery', async () => {
  const fixture = setup({ updateFetch: (_, { signal }) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
  }) });
  const { controller } = fixture;
  try {
    await controller.start();
    assert.equal(controller.snapshot.ready, true);
    await controller.refresh();
    assert.equal(controller.snapshot.ready, true);
    await fixture.tick(15000);
    assert.equal(controller.snapshot.updates.state, 'failed');
    assert.equal(controller.snapshot.error, null);
    await fixture.tick(3600000);
    assert.equal(controller.snapshot.updates.state, 'checking');
  } finally { controller.stop(); }
  await flush();
  assert.equal(fixture.timers.size, 0);
});

test('stop cancels update polling and queued callbacks cannot restart it', async () => {
  const fixture = setup(); const { controller, timers, calls } = fixture;
  await controller.start(); await flush();
  const poll = [...timers.values()].find((timer) => timer.delay === 2000).fn;
  controller.stop();
  poll(); await controller.checkUpdates({ force: true });
  assert.equal(calls.length, 1);
  assert.equal(timers.size, 0);
  await controller.start(); await flush();
  poll(); await flush(); // old generation must not schedule work in the new one
  assert.equal(calls.length, 2);
  controller.stop();
});

for (const rejects of [false, true]) {
  test(`stale update ${rejects ? 'failure' : 'completion'} cannot replace a restarted lifecycle`, async () => {
    const pending = [];
    const fixture = setup({ updateFetch: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
    const { controller, calls, timers } = fixture;
    await controller.start();
    controller.stop();
    assert.equal(calls[0].signal.aborted, true);
    assert.equal(timers.size, 0); // includes the aborted request timeout
    await controller.start();
    pending[1].resolve(response(result('current')));
    await flush();
    if (rejects) pending[0].reject(new Error('late error'));
    else pending[0].resolve(response(result('available', { latest_version: '9.0.0' })));
    await flush();
    assert.equal(controller.snapshot.updates.state, 'current');
    assert.equal(controller.snapshot.ready, true);
    assert.equal([...timers.values()].filter((timer) => timer.delay === 3600000).length, 1);
    controller.stop();
    assert.equal(timers.size, 0);
  });
}
