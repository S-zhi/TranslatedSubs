import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { createCloudTrialController } from '../lib/cloud-trial-controller.js';

export default function useCloudTrial(options) {
  const controller = useMemo(() => createCloudTrialController(options), []);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);

  useEffect(() => {
    controller.start();
    return controller.stop;
  }, [controller]);

  return {
    ...state,
    createTrial: controller.create,
    recheckTrial: controller.recheck,
    endTrial: controller.end,
    canOpenTrial: controller.canOpen,
  };
}
