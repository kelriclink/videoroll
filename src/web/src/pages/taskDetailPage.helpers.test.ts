import { describe, expect, it } from "vitest";

import { createTaskDetailPollPlan, TASK_DETAIL_POLL_INTERVAL_MS, TASK_QUEUE_KICK_INTERVAL_MS } from "./taskDetailPage.helpers";

describe("createTaskDetailPollPlan", () => {
  it("stops polling work when the task is idle", () => {
    expect(
      createTaskDetailPollPlan({
        shouldPoll: false,
        hasQueuedSubtitleJob: false,
        lastQueueKickAt: 0,
        now: 50_000,
      }),
    ).toEqual({
      shouldRefreshTask: false,
      shouldLoadLogs: false,
      shouldKickQueue: false,
      nextDelayMs: null,
    });
  });

  it("refreshes task and logs on each active cycle", () => {
    expect(
      createTaskDetailPollPlan({
        shouldPoll: true,
        hasQueuedSubtitleJob: false,
        lastQueueKickAt: 0,
        now: 50_000,
      }),
    ).toEqual({
      shouldRefreshTask: true,
      shouldLoadLogs: true,
      shouldKickQueue: false,
      nextDelayMs: TASK_DETAIL_POLL_INTERVAL_MS,
    });
  });

  it("rate limits queue kicks to the configured interval", () => {
    const now = 50_000;
    expect(
      createTaskDetailPollPlan({
        shouldPoll: true,
        hasQueuedSubtitleJob: true,
        lastQueueKickAt: now - TASK_QUEUE_KICK_INTERVAL_MS + 1,
        now,
      }).shouldKickQueue,
    ).toBe(false);

    expect(
      createTaskDetailPollPlan({
        shouldPoll: true,
        hasQueuedSubtitleJob: true,
        lastQueueKickAt: now - TASK_QUEUE_KICK_INTERVAL_MS,
        now,
      }).shouldKickQueue,
    ).toBe(true);
  });
});
