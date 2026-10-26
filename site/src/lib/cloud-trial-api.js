const STATUSES = new Set([
  'idle', 'creating', 'booting', 'checking', 'publishing', 'ready',
  'deleting', 'failed', 'expired', 'unavailable',
]);
const WORKBENCH_DOMAINS = ['.instabox.tech', '.instabox.sh'];
const ERROR_MESSAGES = {
  capacity: '当前试用名额已满，请稍后重新检查。',
  budget: '云端试用暂时不可用，请稍后重新检查。',
  unavailable: '云端试用服务暂时不可用，请稍后重新检查。',
  session_required: '试用会话需要重新确认，请检查状态后再试。',
  origin: '无法验证本次请求，请刷新页面后再试。',
  network: '暂时无法连接试用服务，请重新检查状态。',
  invalid_response: '暂时无法确认试用状态，请重新检查。',
  unsafe_url: '暂时无法确认工作台链接，请重新检查状态。',
  poll_timeout: '准备时间较长，已暂停自动检查。请手动重新检查状态。',
  request: '试用请求未完成，请重新检查状态。',
};

export class CloudTrialError extends Error {
  constructor(code = 'request', status = 0) {
    const safeCode = Object.hasOwn(ERROR_MESSAGES, code) ? code : 'request';
    super(ERROR_MESSAGES[safeCode]);
    this.name = 'CloudTrialError';
    this.code = safeCode;
    this.status = status;
  }
}

export function validateWorkbenchUrl(value, { mode = 'isolated', machineId = null } = {}) {
  if (typeof value !== 'string' || /[\s\\\u0000-\u001f\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    const authority = value.match(/^https:\/\/([^/?#]+)/i)?.[1];
    const tokenOkay = mode === 'fixed'
      ? /^$/.test(url.hash) && typeof machineId === 'string' &&
        /^m_[a-z2-7]{26}$/.test(machineId) &&
        url.hostname === `8000-${machineId.slice(2)}.instabox.tech`
      : /^#trial_token=[a-f\d]{64}$/.test(url.hash);
    if (
      url.protocol !== 'https:' || url.username || url.password || url.port ||
      !authority || authority.includes('@') || authority.includes(':') ||
      url.pathname !== '/' || url.search || value.includes('?') ||
      !WORKBENCH_DOMAINS.some((suffix) => url.hostname.endsWith(suffix)) ||
      !tokenOkay
    ) return null;
    return url.href;
  } catch {
    return null;
  }
}

export function normalizeCloudTrial(value) {
  if (!value || typeof value.configured !== 'boolean' || !STATUSES.has(value.status)) {
    throw new CloudTrialError('invalid_response');
  }
  const { trialId = null, expiresAt = null, message = null,
    mode = 'isolated', machineId = null } = value;
  if (
    !['isolated', 'fixed'].includes(mode) ||
    (machineId !== null && (typeof machineId !== 'string' || !/^m_[a-z2-7]{26}$/.test(machineId))) ||
    (mode === 'fixed' && machineId === null) ||
    (trialId !== null && typeof trialId !== 'string') ||
    (message !== null && typeof message !== 'string') ||
    (expiresAt !== null && (
      typeof expiresAt !== 'string' ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(expiresAt) ||
      !Number.isFinite(Date.parse(expiresAt))
    ))
  ) throw new CloudTrialError('invalid_response');

  const url = value.status === 'ready' ? validateWorkbenchUrl(value.url, { mode, machineId }) : null;
  if (value.status === 'ready' && (!value.configured || !url || !trialId ||
      (mode === 'fixed' && trialId !== machineId))) {
    throw new CloudTrialError('unsafe_url');
  }
  return { configured: value.configured, mode, machineId, status: value.status,
    trialId, expiresAt, url, message };
}

export function createCloudTrialApi({ fetchImpl = globalThis.fetch } = {}) {
  async function request(method, path, { signal } = {}) {
    let response;
    try {
      response = await fetchImpl(path, {
        method,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: method === 'GET'
          ? { Accept: 'application/json' }
          : { Accept: 'application/json', 'Content-Type': 'application/json' },
        ...(method === 'GET' ? {} : { body: '{}' }),
        signal,
      });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new CloudTrialError('network');
    }

    let body;
    try {
      body = await response.json();
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new CloudTrialError(response.ok ? 'invalid_response' : 'unavailable', response.status);
    }
    if (!response.ok) {
      throw new CloudTrialError(body?.detail?.code ?? 'unavailable', response.status);
    }
    return normalizeCloudTrial(body);
  }

  return {
    current: (options) => request('GET', '/api/trials/current', options),
    create: (options) => request('POST', '/api/trials', options),
    end: (options) => request('DELETE', '/api/trials/current', options),
  };
}
