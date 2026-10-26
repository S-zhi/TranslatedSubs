import assert from 'node:assert/strict';
import test from 'node:test';
import { CloudTrialError, createCloudTrialApi } from '../src/lib/cloud-trial-api.js';
import { createCloudTrialController, getCloudTrialAction } from '../src/lib/cloud-trial-controller.js';

const link = `https://8000-machine.instabox.tech/#trial_token=${'a'.repeat(64)}`;
const idle = { configured: true, status: 'idle', trialId: null, expiresAt: null, url: null, message: null };
const trial = (status, extra = {}) => ({ ...idle, status, trialId: 'trial-one', ...extra });
const ready = (extra = {}) => trial('ready', { url: link, ...extra });
const flush = async () => { for (let count = 0; count < 25; count += 1) await Promise.resolve(); };

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function fixture(responses, options = {}) {
  const queue = [...responses];
  const calls = [];
  const timers = new Map();
  let clock = Date.parse('2026-10-19T12:00:00Z');
  let timerId = 0;
  const api = createCloudTrialApi({
    fetchImpl: async (path, settings) => {
      calls.push({ path, ...settings });
      assert.ok(queue.length, `Unexpected ${settings.method} ${path}`);
      const result = queue.shift();
      const body = typeof result === 'function' ? await result(settings) : result;
      if (body instanceof Error) throw body;
      return { ok: true, status: 200, json: async () => body };
    },
  });
  const controller = createCloudTrialController({
    api,
    now: () => clock,
    setTimer: (callback, delay) => {
      const id = ++timerId;
      timers.set(id, { at: clock + delay, callback });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    pollIntervalMs: 100,
    requestTimeoutMs: 500,
    ...options,
  });
  async function advance(ms) {
    const end = clock + ms;
    while (true) {
      const next = [...timers.entries()].filter(([, value]) => value.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      clock = next[1].at;
      timers.delete(next[0]);
      next[1].callback();
      await flush();
    }
    clock = end;
    await flush();
  }
  return { controller, calls, timers, advance, setClock: (value) => { clock = value; } };
}

test('mount restores only with GET and idle does not poll or provision', async () => {
  const f = fixture([idle]);
  await f.controller.start();
  await f.advance(10000);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET']);
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'create');
  assert.equal(f.timers.size, 0);
  f.controller.stop();
});

test('StrictMode stop/start aborts stale restoration and never creates', async () => {
  const first = deferred();
  const f = fixture([() => first.promise, ready()]);
  const stale = f.controller.start();
  const firstSignal = f.calls[0].signal;
  f.controller.stop();
  assert.equal(firstSignal.aborted, true);
  assert.equal(f.timers.size, 0);
  await f.controller.start();
  first.resolve(idle);
  await stale;
  assert.equal(f.controller.getSnapshot().status, 'ready');
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'GET']);
  f.controller.stop();
});

test('an explicit hero click can await the initial cookie GET and duplicate clicks still create once', async () => {
  const session = deferred();
  const f = fixture([() => session.promise, trial('creating')]);
  const restore = f.controller.start();
  const clicked = f.controller.create();
  assert.equal(await f.controller.create(), false);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET']);
  session.resolve(idle);
  await restore;
  await clicked;
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'POST']);
  f.controller.stop();
});

test('duplicate create clicks are guarded synchronously before the request resolves', async () => {
  const creation = deferred();
  const f = fixture([idle, () => creation.promise]);
  await f.controller.start();
  const first = f.controller.create();
  assert.equal(await f.controller.create(), false);
  assert.equal(f.calls.length, 2);
  creation.resolve(trial('creating'));
  await first;
  assert.equal(await f.controller.create(), false);
  assert.equal(f.calls.filter(({ method }) => method === 'POST').length, 1);
  f.controller.stop();
});

test('transient server stages poll with GET until ready and then stop', async () => {
  const f = fixture([idle, trial('creating'), trial('booting'), trial('checking'), trial('publishing'), ready()]);
  await f.controller.start();
  await f.controller.create();
  await f.advance(400);
  assert.equal(f.controller.getSnapshot().status, 'ready');
  assert.equal(f.controller.canOpen(), true);
  assert.equal(f.calls.filter(({ method }) => method === 'POST').length, 1);
  assert.equal(f.timers.size, 0);
  await f.advance(10000);
  assert.equal(f.calls.length, 6);
  f.controller.stop();
});

test('fixed mode starts once, polls without expiry, and opens only its machine URL', async () => {
  const machineId = 'm_agqu7uni5qvmvqtn3hufecdujm';
  const host = 'https://8000-agqu7uni5qvmvqtn3hufecdujm.instabox.tech';
  const fixedIdle = { configured: true, mode: 'fixed', machineId, status: 'idle',
    trialId: machineId, expiresAt: null, url: null, message: null };
  const booting = { ...fixedIdle, status: 'booting' };
  const fixedReady = { ...fixedIdle, status: 'ready', url: host };
  const f = fixture([fixedIdle, booting, fixedReady]);
  await f.controller.start();
  assert.equal(await f.controller.create(), true);
  await f.advance(100);
  assert.equal(f.controller.getSnapshot().mode, 'fixed');
  assert.equal(f.controller.getSnapshot().expiresAt, null);
  assert.equal(f.controller.getSnapshot().url, `${host}/`);
  assert.equal(f.controller.canOpen(), true);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'POST', 'GET']);
  f.controller.stop();
});

test('ambiguous POST network failure recovers by GET and never auto-retries creation', async () => {
  const f = fixture([idle, new TypeError('connection lost after send'), trial('booting'), ready()]);
  await f.controller.start();
  await f.controller.create();
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'POST', 'GET']);
  assert.equal(f.controller.getSnapshot().status, 'booting');
  await f.advance(100);
  assert.equal(f.controller.getSnapshot().status, 'ready');
  assert.equal(f.calls.filter(({ method }) => method === 'POST').length, 1);
  f.controller.stop();
});

test('failed recovery pauses polling and requires a GET recheck before a later explicit create', async () => {
  const f = fixture([idle, new TypeError('lost'), new TypeError('offline'), idle, trial('creating')]);
  await f.controller.start();
  await f.controller.create();
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'recheck');
  assert.equal(await f.controller.create(), false);
  await f.advance(1000);
  assert.equal(f.calls.length, 3);
  await f.controller.recheck();
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'create');
  await f.controller.create();
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'POST', 'GET', 'GET', 'POST']);
  f.controller.stop();
});

test('an ambiguous create followed by idle still exposes a GET-only recheck', async () => {
  const f = fixture([idle, new TypeError('lost'), idle]);
  await f.controller.start();
  await f.controller.create();
  assert.equal(f.controller.getSnapshot().status, 'idle');
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'recheck');
  assert.equal(f.timers.size, 0);
  f.controller.stop();
});

test('capacity errors preserve a usable recheck and do not retry POST', async () => {
  const f = fixture([idle], {
    api: {
      current: async () => idle,
      create: async () => { throw new CloudTrialError('capacity', 429); },
    },
  });
  await f.controller.start();
  await f.controller.create();
  assert.equal(f.controller.getSnapshot().error.code, 'capacity');
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'recheck');
  await f.controller.recheck();
  assert.equal(f.controller.getSnapshot().error, null);
  f.controller.stop();
});

test('unconfigured states remain honest and can be rechecked without a POST', async () => {
  const f = fixture([{ ...idle, configured: false, status: 'unavailable' }, idle]);
  await f.controller.start();
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'recheck');
  assert.equal(await f.controller.create(), false);
  await f.controller.recheck();
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'create');
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'GET']);
  f.controller.stop();
});

test('local expiry clears the ready URL at the deadline with no server polling', async () => {
  const f = fixture([ready({ expiresAt: '2026-10-19T12:00:01Z' })]);
  await f.controller.start();
  assert.equal(f.controller.canOpen(), true);
  await f.advance(1000);
  assert.equal(f.controller.getSnapshot().status, 'expired');
  assert.equal(f.controller.getSnapshot().url, null);
  assert.equal(f.controller.canOpen(), false);
  assert.equal(f.calls.length, 1);
  assert.equal(f.timers.size, 0);
  f.controller.stop();
});

test('already expired links are never opened and click-time guard handles a delayed timer', async () => {
  const expired = fixture([ready({ expiresAt: '2026-10-19T11:59:59Z' })]);
  await expired.controller.start();
  assert.equal(expired.controller.getSnapshot().status, 'expired');
  assert.equal(expired.controller.canOpen(), false);
  expired.controller.stop();
  const delayed = fixture([ready({ expiresAt: '2026-10-19T12:00:01Z' })]);
  await delayed.controller.start();
  delayed.setClock(Date.parse('2026-10-19T12:00:02Z'));
  assert.equal(delayed.controller.canOpen(), false);
  assert.equal(delayed.controller.getSnapshot().url, null);
  delayed.controller.stop();
});

test('terminal states never poll and excessive transient polling pauses for manual recheck', async () => {
  for (const status of ['idle', 'failed', 'expired', 'unavailable']) {
    const f = fixture([trial(status)]);
    await f.controller.start();
    await f.advance(10000);
    assert.equal(f.calls.length, 1, status);
    assert.equal(f.timers.size, 0, status);
    f.controller.stop();
  }
  const f = fixture([trial('booting'), trial('booting'), trial('booting'), ready()], { maxPollDurationMs: 300 });
  await f.controller.start();
  await f.advance(300);
  assert.equal(f.controller.getSnapshot().error.code, 'poll_timeout');
  assert.equal(f.timers.size, 0);
  await f.advance(10000);
  assert.equal(f.calls.length, 3);
  await f.controller.recheck();
  assert.equal(f.controller.getSnapshot().status, 'ready');
  f.controller.stop();
});

test('ending a trial during preparation sends a single DELETE and immediately removes its entry', async () => {
  const ending = deferred();
  const f = fixture([trial('booting'), () => ending.promise]);
  await f.controller.start();
  const end = f.controller.end();
  assert.equal(f.controller.getSnapshot().status, 'deleting');
  assert.equal(f.controller.getSnapshot().url, null);
  assert.equal(await f.controller.end(), false);
  ending.resolve(trial('expired'));
  await end;
  assert.equal(f.controller.getSnapshot().status, 'expired');
  assert.equal(f.controller.getSnapshot().ended, true);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'DELETE']);
  assert.equal(f.timers.size, 0);
  f.controller.stop();
});

test('ambiguous DELETE recovery never automatically reopens a ready link', async () => {
  const f = fixture([ready(), new TypeError('response lost'), ready(), trial('expired')]);
  await f.controller.start();
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().url, null);
  assert.equal(f.controller.canOpen(), false);
  assert.equal(getCloudTrialAction(f.controller.getSnapshot()), 'recheck');
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'DELETE', 'GET']);
  await f.controller.recheck();
  assert.equal(f.controller.getSnapshot().status, 'expired');
  assert.equal(f.calls.filter(({ method }) => method === 'DELETE').length, 1);
  f.controller.stop();
});

test('a successful delete can be polled until expired without another DELETE', async () => {
  const f = fixture([ready(), trial('deleting'), trial('expired')]);
  await f.controller.start();
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().status, 'deleting');
  await f.advance(100);
  assert.equal(f.controller.getSnapshot().status, 'expired');
  assert.equal(f.controller.getSnapshot().ended, true);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'DELETE', 'GET']);
  assert.equal(f.timers.size, 0);
  f.controller.stop();
});

test('unmount cancels active requests and timers and ignores late mutation results', async () => {
  const creation = deferred();
  const f = fixture([idle, () => creation.promise]);
  await f.controller.start();
  const pending = f.controller.create();
  const signal = f.calls[1].signal;
  f.controller.stop();
  assert.equal(signal.aborted, true);
  assert.equal(f.timers.size, 0);
  const snapshot = f.controller.getSnapshot();
  creation.resolve(ready());
  await pending;
  assert.equal(f.controller.getSnapshot(), snapshot);
  await f.advance(10000);
  assert.equal(f.calls.length, 2);
});

test('a request timeout aborts POST and recovers only with GET', async () => {
  const f = fixture([
    idle,
    ({ signal }) => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    }),
    ready(),
  ]);
  await f.controller.start();
  const creating = f.controller.create();
  await f.advance(500);
  await creating;
  assert.equal(f.calls[1].signal.aborted, true);
  assert.deepEqual(f.calls.map(({ method }) => method), ['GET', 'POST', 'GET']);
  assert.equal(f.controller.getSnapshot().status, 'ready');
  f.controller.stop();
});
