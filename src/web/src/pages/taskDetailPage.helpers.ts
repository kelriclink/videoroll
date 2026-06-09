export const TASK_DETAIL_POLL_INTERVAL_MS = 2000;

export type TaskDetailPollPlan = {
  shouldRefreshTask: boolean;
  shouldLoadLogs: boolean;
  nextDelayMs: number | null;
};

export function createTaskDetailPollPlan(args: {
  shouldPoll: boolean;
}): TaskDetailPollPlan {
  const { shouldPoll } = args;
  if (!shouldPoll) {
    return {
      shouldRefreshTask: false,
      shouldLoadLogs: false,
      nextDelayMs: null,
    };
  }
  return {
    shouldRefreshTask: true,
    shouldLoadLogs: true,
    nextDelayMs: TASK_DETAIL_POLL_INTERVAL_MS,
  };
}
