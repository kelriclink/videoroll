export const TASK_DETAIL_POLL_INTERVAL_MS = 2000;
export const TASK_QUEUE_KICK_INTERVAL_MS = 10_000;

export type TaskDetailPollPlan = {
  shouldRefreshTask: boolean;
  shouldLoadLogs: boolean;
  shouldKickQueue: boolean;
  nextDelayMs: number | null;
};

export function createTaskDetailPollPlan(args: {
  shouldPoll: boolean;
  hasQueuedSubtitleJob: boolean;
  lastQueueKickAt: number;
  now: number;
}): TaskDetailPollPlan {
  const { shouldPoll, hasQueuedSubtitleJob, lastQueueKickAt, now } = args;
  if (!shouldPoll) {
    return {
      shouldRefreshTask: false,
      shouldLoadLogs: false,
      shouldKickQueue: false,
      nextDelayMs: null,
    };
  }
  return {
    shouldRefreshTask: true,
    shouldLoadLogs: true,
    shouldKickQueue: hasQueuedSubtitleJob && now - lastQueueKickAt >= TASK_QUEUE_KICK_INTERVAL_MS,
    nextDelayMs: TASK_DETAIL_POLL_INTERVAL_MS,
  };
}
