import { describe, expect, it } from "vitest";

import * as helpers from "./taskDetailPage.helpers";
import { buildPublishActionPayload, createTaskDetailPollPlan, TASK_DETAIL_POLL_INTERVAL_MS } from "./taskDetailPage.helpers";

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

describe("buildPublishActionPayload", () => {
  it("includes force_retry for Bilibili retries", () => {
    expect(
      buildPublishActionPayload({
        platform: "bilibili",
        accountId: "",
        videoKey: "final/video.mp4",
        coverKey: "cover.jpg",
        meta: { title: "标题" },
        schedule: "",
        typeidMode: "ai_summary",
        skipReview: false,
        forceRetry: true,
      }),
    ).toMatchObject({ force_retry: true });
  });

  it("builds a social request without Bilibili typeid options", () => {
    expect(
      buildPublishActionPayload({
        platform: "douyin",
        accountId: "account-1",
        videoKey: "final/video.mp4",
        coverKey: "cover.jpg",
        meta: { title: "标题", desc: "简介", tags: ["一"] },
        schedule: "2026-07-11 20:30",
        typeidMode: "ai_summary",
        skipReview: false,
      }),
    ).toEqual({
      platform: "douyin",
      account_id: "account-1",
      video_key: "final/video.mp4",
      cover_key: "cover.jpg",
      meta: { title: "标题", desc: "简介", tags: ["一"] },
      platform_options: { douyin: { schedule: "2026-07-11 20:30" } },
      skip_review: false,
      force_retry: false,
    });
  });
});

describe("socialPublishBrowserUrl", () => {
  it("exposes the worker noVNC desktop only for Douyin", () => {
    const socialPublishBrowserUrl = (helpers as any).socialPublishBrowserUrl;
    expect(typeof socialPublishBrowserUrl).toBe("function");
    expect(socialPublishBrowserUrl("douyin")).toBe(
      "/social-publish/vnc.html?autoconnect=1&resize=scale&path=social-publish/websockify",
    );
    expect(socialPublishBrowserUrl("xiaohongshu")).toBeNull();
    expect(socialPublishBrowserUrl("kuaishou")).toBeNull();
    expect(socialPublishBrowserUrl("bilibili")).toBeNull();
  });
});
