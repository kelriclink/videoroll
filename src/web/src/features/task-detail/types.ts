import type { Asset, PublishBatch, PublishJob, SubtitleJob, Task, TaskCoreSnapshot } from "../../lib/types";
import type { PublishPlatformSettings, SocialAccount } from "../../lib/publish";

export type { Asset, PublishBatch, PublishJob, SubtitleJob, Task, TaskCoreSnapshot };
export type { PublishPlatformSettings, SocialAccount };
export type { DesktopGrant } from "../../api/desktop";
export type {
  BilibiliArchiveTypesResponse,
  BiliTypeNode,
  BilibiliTypeRecommendResponse,
  PublishMetaDraftResponse,
  PublishMetaStoreResponse,
  PublishResponse,
  PublishReview,
} from "../../api/publish";
export type {
  SubtitleActionResponse,
  SubtitleAutoProfile,
  YouTubeSubtitleMode,
} from "../../api/subtitle";
export type {
  YouTubeDownloadActionResponse,
  YouTubeDownloadProgress,
  YouTubeMeta,
  YouTubeMetaActionResponse,
} from "../../api/tasks";

export type PublishPlatformSettingsResponse = { platforms: PublishPlatformSettings };
export type WorkflowStepState = "done" | "active" | "pending" | "failed";
export type WorkflowStep = { label: string; detail: string; state: WorkflowStepState };
export type TaskDetailTab = "overview" | "media" | "subtitle" | "publish" | "logs";
