import { createContext, useContext } from 'react';
import { ArrowUpRightIcon } from '@phosphor-icons/react';
import GetStartedSection from './GetStartedSection.jsx';
import useCloudTrial from '../hooks/use-cloud-trial.js';
import { getCloudTrialAction } from '../lib/cloud-trial-controller.js';
import './CloudTrialSlot.css';

const CloudTrialContext = /* @__PURE__ */ createContext(null);

export function CloudTrialProvider({ children }) {
  const trial = useCloudTrial();
  return <CloudTrialContext.Provider value={trial}>{children}</CloudTrialContext.Provider>;
}

export function TrialAction({ reducedMotion }) {
  const trial = useContext(CloudTrialContext);
  const trialAction = getCloudTrialAction(trial);

  if (trialAction === 'open') {
    return (
      <a
        className="button button--primary"
        href={trial.url}
        target="_blank"
        rel="noreferrer"
        onClick={(event) => {
          if (!trial.canOpenTrial()) event.preventDefault();
        }}
      >
        打开我的工作台 <ArrowUpRightIcon size={19} weight="bold" aria-hidden="true" />
      </a>
    );
  }

  const label = trial.configured === null && !trial.error
    ? '快速使用'
    : trialAction === 'wait'
      ? '查看准备进度'
      : trialAction === 'recheck'
        ? '检查云端试用状态'
        : '快速使用';

  return (
    <button
      className="button button--primary"
      type="button"
      onClick={() => {
        if (trial.configured === null && !trial.error) void trial.createTrial();
        else if (trialAction === 'create') void trial.createTrial();
        else if (trialAction === 'recheck') void trial.recheckTrial();
        document.getElementById('cloud-start')?.scrollIntoView({
          behavior: reducedMotion ? 'instant' : 'smooth',
          block: 'start',
        });
      }}
    >
      {label} <ArrowUpRightIcon size={19} weight="bold" aria-hidden="true" />
    </button>
  );
}

export function GetStartedSectionWithTrial() {
  const trial = useContext(CloudTrialContext);
  return <GetStartedSection trial={trial} onStartTrial={trial.createTrial} />;
}
