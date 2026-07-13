export const TASK_DETAIL_POLL_INTERVAL_MS = 2000;

export type PublishPlatform = "bilibili" | "douyin" | "xiaohongshu" | "kuaishou";

export function socialPublishBrowserUrl(platform: PublishPlatform): string | null {
  if (platform !== "douyin") return null;
  return "/social-publish/vnc.html?autoconnect=1&resize=scale&path=social-publish/websockify";
}

export function buildPublishActionPayload(args: {
  platform: PublishPlatform;
  accountId: string;
  videoKey: string;
  coverKey: string;
  meta: Record<string, unknown>;
  schedule: string;
  typeidMode: string;
  skipReview: boolean;
  forceRetry?: boolean;
}): PublishActionPayload {
  if (args.platform === "bilibili") {
    return {
      platform: args.platform,
      account_id: args.accountId || null,
      video_key: args.videoKey || null,
      cover_key: args.coverKey || null,
      typeid_mode: args.typeidMode,
      meta: args.meta,
      platform_options: { bilibili: { typeid_mode: args.typeidMode } },
      skip_review: args.skipReview,
      force_retry: Boolean(args.forceRetry),
    };
  }
  return {
    platform: args.platform,
    account_id: args.accountId,
    video_key: args.videoKey || null,
    cover_key: args.coverKey || null,
    meta: args.meta,
    platform_options: {
      [args.platform]: args.schedule ? { schedule: args.schedule } : {},
    },
    skip_review: args.skipReview,
    force_retry: Boolean(args.forceRetry),
  };
}

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
import { PublishActionPayload } from "../lib/types";
