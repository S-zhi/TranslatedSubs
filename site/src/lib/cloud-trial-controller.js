import { CloudTrialError, createCloudTrialApi, validateWorkbenchUrl } from './cloud-trial-api.js';

export const TRANSIENT_TRIAL_STATUSES = new Set(['creating', 'booting', 'checking', 'publishing', 'deleting']);
const EXPIRABLE_STATUSES = new Set(['creating', 'booting', 'checking', 'publishing', 'ready']);
const INITIAL_STATE = {
  configured: null,
  mode: 'isolated',
  machineId: null,
  status: 'idle',
  trialId: null,
  expiresAt: null,
  url: null,
  message: null,
  pending: false,
  error: null,
  needsRecheck: false,
  ended: false,
};

export function createCloudTrialController({
  api = createCloudTrialApi(),
  now = Date.now,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  pollIntervalMs = 2500,
  maxPollDurationMs = 10 * 60 * 1000,
  requestTimeoutMs = 20000,
} = {}) {
  let state = { ...INITIAL_STATE };
  let active = false;
  let generation = 0;
  let operation = null;
  let initialRestore = null;
  let creationIntent = null;
  let pollTimer = null;
  let expiryTimer = null;
  let pollDeadline = null;
  const listeners = new Set();

  function publish(patch) {
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener());
  }

  function clearPoll() {
    if (pollTimer !== null) clearTimer(pollTimer);
    pollTimer = null;
  }

  function clearExpiry() {
    if (expiryTimer !== null) clearTimer(expiryTimer);
    expiryTimer = null;
  }

  function expireIfNeeded() {
    if (
      state.expiresAt && EXPIRABLE_STATUSES.has(state.status) &&
      Date.parse(state.expiresAt) <= now()
    ) {
      clearPoll();
      clearExpiry();
      pollDeadline = null;
      publish({ status: 'expired', url: null, error: null, needsRecheck: false });
      return true;
    }
    return false;
  }

  function schedule() {
    clearPoll();
    clearExpiry();
    if (!active || expireIfNeeded()) return;
    if (state.expiresAt && EXPIRABLE_STATUSES.has(state.status)) {
      const remaining = Date.parse(state.expiresAt) - now();
      expiryTimer = setTimer(() => {
        expiryTimer = null;
        if (!expireIfNeeded()) schedule();
      }, Math.min(remaining, 2147483647));
    }
    if (!TRANSIENT_TRIAL_STATUSES.has(state.status)) {
      pollDeadline = null;
      return;
    }
    if (state.configured !== true || state.pending || state.needsRecheck) return;
    if (pollDeadline === null) pollDeadline = now() + maxPollDurationMs;
    const remaining = pollDeadline - now();
    if (remaining <= 0) {
      publish({ error: new CloudTrialError('poll_timeout'), needsRecheck: true });
      return;
    }
    pollTimer = setTimer(() => {
      pollTimer = null;
      if (now() >= pollDeadline) {
        publish({ error: new CloudTrialError('poll_timeout'), needsRecheck: true });
      } else {
        void run('current');
      }
    }, Math.min(pollIntervalMs, remaining));
  }

  function isCurrent(task) {
    return active && operation === task && generation === task.generation;
  }

  function accept(value) {
    const ended = value.status === 'expired' && (state.ended || state.status === 'deleting');
    publish({ ...value, error: null, needsRecheck: false, ended });
    expireIfNeeded();
  }

  function recordError(error) {
    const safeError = error instanceof CloudTrialError ? error : new CloudTrialError('network');
    publish({ error: safeError, url: null, needsRecheck: true });
  }

  async function request(task, method) {
    task.controller = new AbortController();
    task.timedOut = false;
    task.timeout = setTimer(() => {
      task.timedOut = true;
      task.controller.abort();
    }, requestTimeoutMs);
    try {
      return await api[method]({ signal: task.controller.signal });
    } catch (error) {
      if (task.timedOut) throw new CloudTrialError('network');
      throw error;
    } finally {
      clearTimer(task.timeout);
      task.timeout = null;
    }
  }

  async function run(method) {
    if (!active || operation) return false;
    const beforeMutation = state;
    const task = { generation, controller: null, timeout: null };
    operation = task; // Synchronous lock: a second click cannot start another request.
    clearPoll();
    publish({ pending: true, error: null, needsRecheck: false });
    if (method === 'create') {
      clearExpiry();
      pollDeadline = null;
      publish({ status: 'creating', trialId: null, expiresAt: null, url: null, ended: false });
    } else if (method === 'end') {
      clearExpiry();
      pollDeadline = null;
      publish({ status: 'deleting', ended: true, url: null });
    }
    try {
      const value = await request(task, method);
      if (isCurrent(task)) accept(value);
    } catch (error) {
      if (!isCurrent(task)) return false;
      const ambiguous = !(error instanceof CloudTrialError) || !error.status || error.status >= 500;
      if (method !== 'current' && ambiguous) {
        // The mutation may have reached the service. Recover only by reading it.
        try {
          const value = await request(task, 'current');
          if (!isCurrent(task)) return false;
          accept(value);
          if (value.status === 'idle' && method === 'create') recordError(error);
          if (method === 'end' && value.status === 'ready') recordError(error);
        } catch (recoveryError) {
          if (isCurrent(task)) recordError(recoveryError);
        }
      } else {
        if (method !== 'current') publish({ ...beforeMutation, pending: true });
        recordError(error);
      }
    } finally {
      if (isCurrent(task)) {
        operation = null;
        publish({ pending: false });
        schedule();
      }
    }
    return true;
  }

  function stop() {
    active = false;
    generation += 1;
    clearPoll();
    clearExpiry();
    if (operation?.timeout !== null && operation?.timeout !== undefined) clearTimer(operation.timeout);
    operation?.controller?.abort();
    operation = null;
    creationIntent = null;
    initialRestore = null;
  }

  function start() {
    if (active) return initialRestore;
    active = true;
    generation += 1;
    pollDeadline = null;
    initialRestore = run('current');
    return initialRestore;
  }

  function recheck() {
    if (operation) return Promise.resolve(false);
    pollDeadline = null;
    return run('current');
  }

  async function create() {
    if (!active || creationIntent) return false;
    const intent = { generation };
    creationIntent = intent; // Also protects an explicit click waiting for the session GET.
    try {
      if (state.configured === null && initialRestore) await initialRestore;
      if (!active || generation !== intent.generation) return false;
      expireIfNeeded();
      if (
        state.configured !== true || state.needsRecheck || state.status === 'ready' ||
        TRANSIENT_TRIAL_STATUSES.has(state.status)
      ) return false;
      return await run('create');
    } finally {
      if (creationIntent === intent) creationIntent = null;
    }
  }

  function end() {
    if (!state.trialId || state.status === 'deleting' || state.status === 'expired') {
      return Promise.resolve(false);
    }
    return run('end');
  }

  function canOpen() {
    return !expireIfNeeded() && !state.needsRecheck && state.status === 'ready' &&
      Boolean(validateWorkbenchUrl(state.url, { mode: state.mode, machineId: state.machineId }));
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    start,
    stop,
    create,
    recheck,
    end,
    canOpen,
  };
}

export function getCloudTrialAction(state) {
  if (state.pending || (state.configured === null && !state.error)) return 'wait';
  if (state.needsRecheck || state.configured !== true || state.status === 'unavailable') return 'recheck';
  if (state.status === 'ready' && validateWorkbenchUrl(state.url, { mode: state.mode, machineId: state.machineId })) return 'open';
  if (TRANSIENT_TRIAL_STATUSES.has(state.status)) return 'wait';
  return 'create';
}
