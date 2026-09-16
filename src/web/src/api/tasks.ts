import { fetchJson, fetchText } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";
import type { Asset, PublishBatch, PublishJob, SubtitleJob, Task } from "../lib/types";
import type { PublishReview } from "./publish";

export type YouTubeMeta = {
  title: string;
  description: string;
  webpage_url: string;
  uploader?: string | null;
  upload_date?: string | null;
  duration?: number | null;
};

export type YouTubeMetaActionResponse = { metadata: YouTubeMeta };
export type YouTubeDownloadActionResponse = {
  metadata: YouTubeMeta;
  video_asset: Asset;
  metadata_asset: Asset;
  cover_asset?: Asset | null;
};
export type YouTubeDownloadProgress = {
  task_id: string;
  status: "idle" | "preparing" | "downloading" | "processing" | "uploading" | "completed" | "failed";
  active: boolean;
  progress: number;
  downloaded_bytes: number;
  total_bytes?: number | null;
  speed_bytes_per_second?: number | null;
  eta_seconds?: number | null;
  filename?: string | null;
  error?: string | null;
  updated_at?: string | null;
};
export type PlayoutAssetLink = {
  id: string;
  status: "ready";
  task_id: string;
  asset_id: string;
  ffplayout_channel_id: number;
  playout_path: string;
  relative_media_path: string;
  transfer_mode: "hardlink" | "copy";
  source_checksum: string;
  created_at: string;
  updated_at: string;
};

export const tasksApi = {
  get(taskId: string) {
    return fetchJson<Task>(orchestratorUrl(`/tasks/${taskId}`));
  },

  assets(taskId: string) {
    return fetchJson<Asset[]>(orchestratorUrl(`/tasks/${taskId}/assets`));
  },

  playoutAssets(taskId: string) {
    return fetchJson<PlayoutAssetLink[]>(orchestratorUrl(`/tasks/${taskId}/playout-assets`));
  },

  addToPlayout(taskId: string, assetId: string) {
    return fetchJson<PlayoutAssetLink>(orchestratorUrl(`/tasks/${taskId}/assets/${assetId}/playout`), { method: "POST" });
  },

  subtitleJobs(taskId: string) {
    return fetchJson<SubtitleJob[]>(orchestratorUrl(`/tasks/${taskId}/subtitle_jobs`));
  },

  publishJobs(taskId: string) {
    return fetchJson<PublishJob[]>(orchestratorUrl(`/tasks/${taskId}/publish_jobs`));
  },

  publishBatches(taskId: string) {
    return fetchJson<PublishBatch[]>(orchestratorUrl(`/tasks/${taskId}/publish_batches`));
  },

  publishReview(taskId: string) {
    return fetchJson<PublishReview>(orchestratorUrl(`/tasks/${taskId}/publish_review`));
  },

  stop(taskId: string) {
    return fetchJson<Task>(orchestratorUrl(`/tasks/${taskId}/actions/stop`), { method: "POST" });
  },

  resume(taskId: string) {
    return fetchJson<Task>(orchestratorUrl(`/tasks/${taskId}/actions/resume`), { method: "POST" });
  },

  youtubeDownload(taskId: string) {
    return fetchJson<YouTubeDownloadActionResponse>(orchestratorUrl(`/tasks/${taskId}/actions/youtube_download`), { method: "POST" });
  },

  youtubeMeta(taskId: string) {
    return fetchJson<YouTubeMetaActionResponse>(orchestratorUrl(`/tasks/${taskId}/actions/youtube_meta`), { method: "POST" });
  },

  uploadVideo(taskId: string, file: File) {
    const body = new FormData();
    body.append("file", file, file.name);
    return fetchJson(orchestratorUrl(`/tasks/${taskId}/upload/video`), { method: "POST", body });
  },

  uploadCover(taskId: string, file: File) {
    const body = new FormData();
    body.append("file", file, file.name);
    return fetchJson<Asset>(orchestratorUrl(`/tasks/${taskId}/upload/cover`), { method: "POST", body });
  },

  deleteAsset(taskId: string, assetId: string) {
    return fetchJson(orchestratorUrl(`/tasks/${taskId}/assets/${assetId}`), { method: "DELETE" });
  },

  youtubeDownloadProgress(taskId: string) {
    return fetchJson<YouTubeDownloadProgress>(orchestratorUrl(`/tasks/${taskId}/youtube_download_progress`));
  },

  assetDownloadUrl(taskId: string, assetId: string) {
    return orchestratorUrl(`/tasks/${taskId}/assets/${assetId}/download`);
  },

  assetText(taskId: string, assetId: string, maxBytes: number) {
    return fetchText(orchestratorUrl(`/tasks/${taskId}/assets/${assetId}/stream`), {
      headers: { Range: `bytes=-${maxBytes}` },
    });
  },
};
