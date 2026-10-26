import assert from 'node:assert/strict';
import test from 'node:test';
import {
  CloudTrialError,
  createCloudTrialApi,
  normalizeCloudTrial,
  validateWorkbenchUrl,
} from '../src/lib/cloud-trial-api.js';

const token = 'a'.repeat(64);
const link = `https://8000-machine.instabox.tech/#trial_token=${token}`;
const idle = { configured: true, mode: 'isolated', machineId: null,
  status: 'idle', trialId: null, expiresAt: null, url: null, message: null };

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test('client uses only same-origin endpoints and empty JSON mutations with AbortSignal', async () => {
  const calls = [];
  const api = createCloudTrialApi({
    fetchImpl: async (...args) => { calls.push(args); return response(idle); },
  });
  const signal = new AbortController().signal;
  await api.current({ signal });
  await api.create({ signal });
  await api.end({ signal });
  assert.deepEqual(calls.map(([path, options]) => [path, options.method, options.body]), [
    ['/api/trials/current', 'GET', undefined],
    ['/api/trials', 'POST', '{}'],
    ['/api/trials/current', 'DELETE', '{}'],
  ]);
  for (const [, options] of calls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.signal, signal);
  }
  assert.equal(calls[1][1].headers['Content-Type'], 'application/json');
  assert.equal(calls[2][1].headers['Content-Type'], 'application/json');
});

test('safe root workbench links require approved domains and an exact lowercase token', () => {
  assert.equal(validateWorkbenchUrl(link), link);
  assert.equal(validateWorkbenchUrl(`https://machine.instabox.sh/#trial_token=${token}`), `https://machine.instabox.sh/#trial_token=${token}`);
  for (const invalid of [
    `http://machine.instabox.tech/#trial_token=${token}`,
    `https://instabox.tech/#trial_token=${token}`,
    `https://machine.instabox.tech.evil.test/#trial_token=${token}`,
    `https://evil.test/#trial_token=${token}`,
    `https://user:pass@machine.instabox.tech/#trial_token=${token}`,
    `https://@machine.instabox.tech/#trial_token=${token}`,
    `https://machine.instabox.tech:444/#trial_token=${token}`,
    `https://machine.instabox.tech:443/#trial_token=${token}`,
    `https://machine.instabox.tech/workbench#trial_token=${token}`,
    `https://machine.instabox.tech/?redirect=evil#trial_token=${token}`,
    `https://machine.instabox.tech/?#trial_token=${token}`,
    `https://machine.instabox.tech/#trial_token=${token}&extra=1`,
    `https://machine.instabox.tech/#trial_token=${'A'.repeat(64)}`,
    `https://machine.instabox.tech/#trial_token=${'a'.repeat(63)}`,
    `https://machine.instabox.tech/#trial_token=${'g'.repeat(64)}`,
    `javascript:alert(1)`,
    `//machine.instabox.tech/#trial_token=${token}`,
    ` https://machine.instabox.tech/#trial_token=${token}`,
    `https:\\machine.instabox.tech/#trial_token=${token}`,
    null,
  ]) assert.equal(validateWorkbenchUrl(invalid), null, String(invalid));
});

test('fixed workbench links must match the configured machine host and omit tokens', () => {
  const machineId = 'm_agqu7uni5qvmvqtn3hufecdujm';
  const fixedUrl = 'https://8000-agqu7uni5qvmvqtn3hufecdujm.instabox.tech';
  assert.equal(validateWorkbenchUrl(fixedUrl, { mode: 'fixed', machineId }), `${fixedUrl}/`);
  assert.equal(validateWorkbenchUrl(`${fixedUrl}/#trial_token=${token}`, { mode: 'fixed', machineId }), null);
  assert.equal(validateWorkbenchUrl('https://8000-other.instabox.tech/', { mode: 'fixed', machineId }), null);
  assert.equal(validateWorkbenchUrl(fixedUrl, { mode: 'fixed', machineId: null }), null);

  const ready = normalizeCloudTrial({ ...idle, mode: 'fixed', machineId, trialId: machineId,
    status: 'ready', url: fixedUrl });
  assert.equal(ready.url, `${fixedUrl}/`);
  assert.throws(() => normalizeCloudTrial({ ...idle, mode: 'fixed', machineId,
    trialId: machineId, status: 'ready', url: `${fixedUrl}/#trial_token=${token}` }), CloudTrialError);
});

test('ready data never exposes an unvalidated URL and unneeded response fields are dropped', () => {
  const valid = { ...idle, status: 'ready', trialId: 'trial-one', url: link, expiresAt: '2026-10-19T16:30:00Z' };
  assert.deepEqual(normalizeCloudTrial({ ...valid, privateKey: 'should-not-pass' }), valid);
  assert.equal(normalizeCloudTrial({ ...idle, url: 'https://evil.test/' }).url, null);
  for (const invalid of [
    { ...valid, url: 'https://evil.test/' },
    { ...valid, configured: false },
    { ...valid, trialId: null },
    { ...idle, status: 'unknown' },
    { ...idle, configured: 'true' },
    { ...idle, expiresAt: 'tomorrow' },
    { ...idle, expiresAt: '2026-10-19T16:30:00+08:00' },
    { ...idle, message: {} },
  ]) assert.throws(() => normalizeCloudTrial(invalid), CloudTrialError);
  assert.equal(normalizeCloudTrial({ ...valid, expiresAt: '2026-10-19T16:30:00+00:00' }).expiresAt, '2026-10-19T16:30:00+00:00');
});

test('HTTP errors retain known codes without reflecting arbitrary server content', async () => {
  const api = createCloudTrialApi({ fetchImpl: async () => response({ detail: { code: 'capacity', message: 'secret server diagnostic' } }, 429) });
  await assert.rejects(api.create(), (error) => {
    assert.equal(error.code, 'capacity');
    assert.equal(error.status, 429);
    assert.doesNotMatch(error.message, /secret/);
    return true;
  });
  const unavailable = createCloudTrialApi({ fetchImpl: async () => response({ detail: { code: 'unexpected', message: 'private information' } }, 503) });
  await assert.rejects(unavailable.current(), (error) => error.code === 'request' && !error.message.includes('private'));
});

test('network and malformed JSON errors are safe while explicit aborts propagate', async () => {
  const network = createCloudTrialApi({ fetchImpl: async () => { throw new TypeError('secret endpoint failure'); } });
  await assert.rejects(network.current(), (error) => error.code === 'network' && !error.message.includes('secret'));
  const malformed = createCloudTrialApi({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError(); } }) });
  await assert.rejects(malformed.current(), (error) => error.code === 'invalid_response');
  const abort = new DOMException('Aborted', 'AbortError');
  const cancelled = createCloudTrialApi({ fetchImpl: async () => { throw abort; } });
  await assert.rejects(cancelled.current(), (error) => error === abort);
});
