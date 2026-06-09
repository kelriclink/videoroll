import { describe, expect, it } from "vitest";

import { createTaskDetailPollPlan, TASK_DETAIL_POLL_INTERVAL_MS } from "./taskDetailPage.helpers";

describe("createTaskDetailPollPlan", () => {
  it("stops polling work when the task is idle", () => {
    expect(
      createTaskDetailPollPlan({
        shouldPoll: false,
      }),
    ).toEqual({
      shouldRefreshTask: false,
      shouldLoadLogs: false,
      nextDelayMs: null,
    });
  });

  it("refreshes task and logs on each active cycle", () => {
    expect(
      createTaskDetailPollPlan({
        shouldPoll: true,
      }),
    ).toEqual({
      shouldRefreshTask: true,
      shouldLoadLogs: true,
      nextDelayMs: TASK_DETAIL_POLL_INTERVAL_MS,
    });
  });
});
