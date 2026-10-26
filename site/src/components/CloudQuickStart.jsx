import {
  ArrowClockwiseIcon,
  ArrowUpRightIcon,
  CheckCircleIcon,
  CircleNotchIcon,
  CloudArrowUpIcon,
  ProhibitInsetIcon,
  TrashIcon,
} from '@phosphor-icons/react';
import { getCloudTrialAction } from '../lib/cloud-trial-controller.js';
import './CloudQuickStart.css';

const STAGES = [
  { status: 'creating', title: '正在创建专属试用环境' },
  { status: 'booting', title: '正在启动基础服务' },
  { status: 'checking', title: '正在检查工作台状态' },
  { status: 'publishing', title: '正在发布安全访问链接' },
];

function formatExpiry(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return null;
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
    timeZoneName: 'short',
  }).format(new Date(value));
}

function getHeading(state) {
  if (state.configured === null && !state.error) return '正在检查云端试用';
  if (state.configured === false) return '云端试用暂未开放';
  if (state.error?.code === 'capacity') return '当前试用名额已满';
  if (state.error?.code === 'budget') return '云端试用暂时不可用';
  if (state.error?.code === 'session_required') return '试用会话需要重新确认';
  if (state.error) return '无法确认试用状态';
  if (state.mode === 'fixed' && state.status === 'ready') return '云端工作台已就绪。';
  if (state.mode === 'fixed' && state.status === 'idle') return '启动云端工作台';
  if (state.status === 'ready') return '你的工作台已准备好。';
  if (state.status === 'deleting') return '正在结束试用并删除数据';
  if (state.status === 'expired') return state.ended ? '试用已结束' : '本次试用已到期';
  if (state.status === 'failed') return '这次试用没有成功启动';
  if (state.status === 'unavailable') return '云端试用暂时不可用';
  return '先在云端体验 TranslatedSubs。';
}

function getDescription(state) {
  if (state.mode === 'fixed') {
    if (state.error) return '请重新检查工作台状态，网络恢复后可以继续启动或打开工作台。';
    if (state.status === 'ready') return '工作台正在运行，打开即可继续使用。';
    if (state.status === 'booting' || state.status === 'checking') return '正在启动并检查云端工作台，请稍候。';
    if (state.status === 'failed' || state.status === 'unavailable') return '当前无法启动工作台，请稍后重试。';
    return '点击后启动现有云端工作台；页面只会检查状态，不会自动启动。';
  }
  if (state.configured === false) return '云端试用服务正在准备中。你仍可查看下方的自托管方式。';
  if (state.error) return '请重新检查当前试用状态。网络恢复后，可继续查看进度或打开工作台。';
  if (state.status === 'ready') return '环境已启动，打开工作台即可开始探索。试用到期后链接将立即失效。';
  if (state.status === 'expired') return state.ended
    ? '工作台链接已失效，服务将停止环境并删除其中的数据。'
    : '工作台链接已失效，试用环境及其中的数据会按服务规则删除。';
  if (state.status === 'deleting') return '正在停止环境并删除其中的工作文件与数据，请稍候。';
  if (state.status === 'failed') return '可以重新检查状态，或再次创建试用环境。';
  return '无需 InstaBox 账号或终端命令。点击后创建短期专属环境，准备好即可打开工作台。';
}

export default function CloudQuickStart({ trial, onStart }) {
  const state = trial;
  const action = getCloudTrialAction(state);
  const fixed = state.mode === 'fixed';
  const expires = fixed ? null : formatExpiry(state.expiresAt);
  const stages = fixed
    ? [
      { status: 'booting', title: '正在启动现有云端工作台' },
      { status: 'checking', title: '正在检查工作台状态' },
    ]
    : STAGES;
  const currentStage = stages.findIndex(({ status }) => status === state.status);
  const canOpen = action === 'open';
  const primaryAction = action === 'recheck'
    ? { label: '重新检查状态', icon: <ArrowClockwiseIcon size={18} aria-hidden="true" />, onClick: state.recheckTrial }
    : action === 'wait'
      ? { label: state.configured === null ? '正在检查试用状态' : '正在准备工作台', icon: <CircleNotchIcon className="cloud-start__spinner" size={18} aria-hidden="true" />, onClick: null }
      : { label: fixed ? '启动云端工作台' : '开始云端试用', icon: <CloudArrowUpIcon size={18} aria-hidden="true" />, onClick: onStart };

  return (
    <aside className="cloud-start" id="cloud-start" aria-labelledby="cloud-start-title">
      <div className="cloud-start__intro">
        <span className="cloud-start__eyebrow">{fixed ? '云端工作台' : '云端快速体验 · 试用'}</span>
        <h3 id="cloud-start-title" aria-live="polite">{getHeading(state)}</h3>
        <p>{getDescription(state)}</p>
        {canOpen ? (
          <a
            className="button button--primary"
            href={state.url}
            target="_blank"
            rel="noreferrer"
            onClick={(event) => {
              if (!state.canOpenTrial()) event.preventDefault();
            }}
          >
            打开我的工作台 <ArrowUpRightIcon size={18} aria-hidden="true" />
          </a>
        ) : (
          <button
            className="button button--primary"
            type="button"
            onClick={primaryAction.onClick}
            disabled={!primaryAction.onClick}
          >
            {primaryAction.icon}{primaryAction.label}
          </button>
        )}
        {state.configured === false && (
          <p className="cloud-start__availability" role="status">
            <ProhibitInsetIcon size={16} aria-hidden="true" /> 云端服务尚未开放
          </p>
        )}
        {state.error && (
          <p className="cloud-start__error" role="alert">{state.error.message}</p>
        )}
      </div>

      <div className="cloud-start__setup">
        {currentStage >= 0 && (
          <ol className="cloud-start__stages" aria-label="云端工作台准备进度" aria-live="polite">
            {stages.map(({ status, title }, index) => {
              const done = index < currentStage;
              const current = index === currentStage;
              return (
                <li
                  className={`cloud-start__stage${done ? ' is-complete' : ''}${current ? ' is-current' : ''}`}
                  key={status}
                  aria-current={current ? 'step' : undefined}
                >
                  <span className="cloud-start__stage-icon" aria-hidden="true">
                    {done
                      ? <CheckCircleIcon size={18} weight="fill" />
                      : current
                        ? <CircleNotchIcon className="cloud-start__spinner" size={18} />
                        : <span>{index + 1}</span>}
                  </span>
                  <strong>{title}</strong>
                </li>
              );
            })}
          </ol>
        )}

        <ul className="cloud-start__promises">
          <li><CheckCircleIcon size={18} aria-hidden="true" />{fixed ? '点击后启动现有云端工作台' : '点击后才会创建试用环境'}</li>
          <li><CheckCircleIcon size={18} aria-hidden="true" />无需 InstaBox 账号或终端命令</li>
          {fixed
            ? <li><CheckCircleIcon size={18} aria-hidden="true" />继续使用同一个云端工作台及其中的现有数据</li>
            : <li><CheckCircleIcon size={18} aria-hidden="true" />短期专属环境，可主动结束并开始清理数据</li>}
        </ul>

        {state.status === 'ready' && expires && (
          <p className="cloud-start__expiry">试用链接到期时间：{expires}</p>
        )}
        {!fixed && state.trialId && ['creating', 'booting', 'checking', 'publishing', 'ready'].includes(state.status) && (
          <button
            className="cloud-start__end-button"
            type="button"
            onClick={state.endTrial}
            disabled={state.pending}
          >
            <TrashIcon size={16} aria-hidden="true" />
            结束试用并删除所有数据
          </button>
        )}
        <p className="cloud-start__note">
          {fixed
            ? '云端工作台提供基础功能。开始处理字幕前，仍需在工作台中配置 Whisper 模型与翻译引擎。'
            : '试用服务只提供基础工作台。开始处理字幕前，仍需在工作台中配置 Whisper 模型与翻译引擎；到期或主动结束时，将停止试用环境并删除其中的数据。'}
        </p>
      </div>
    </aside>
  );
}
