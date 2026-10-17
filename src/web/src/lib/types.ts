export type TaskStatus =
  | "CREATED"
  | "INGESTED"
  | "DOWNLOADED"
  | "AUDIO_EXTRACTED"
  | "ASR_DONE"
  | "TRANSLATED"
  | "SUBTITLE_READY"
  | "RENDERED"
  | "READY_FOR_REVIEW"
  | "APPROVED"
  | "PUBLISHING"
  | "PUBLISHED"
  | "FAILED"
  | "CANCELED";

export type SourceType = "youtube" | "local" | "url";
export type SourceLicense = "own" | "authorized" | "cc" | "unknown";

export type Task = {
  id: string;
  source_type: SourceType;
  source_url?: string | null;
  source_license: SourceLicense;
  source_proof_url?: string | null;
  status: TaskStatus;
  stopped_status?: TaskStatus | null;
  priority: number;
  created_by?: string | null;
  display_title?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retry_count: number;
  bilibili_upload?: {
    job_id: string;
    progress: number;
  } | null;
  created_at: string;
  updated_at: string;
};

export type Asset = {
  task_id?: string;
  id: string;
  kind: string;
  storage_key: string;
  sha256?: string | null;
  size_bytes?: number | null;
  duration_ms?: number | null;
  created_at: string;
};

export type SubtitleJob = {
  id: string;
  task_id: string;
  status: string;
  progress: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type DubbingJob = {
  id: string;
  task_id: string;
  subtitle_job_id: string | null;
  status: "queued" | "preparing" | "synthesizing" | "aligning" | "mixing" | "succeeded" | "failed" | "canceled" | "pause_requested" | "cancel_requested" | "paused" | "preview_ready" | "needs_review";
  progress: number;
  operation_key: string | null;
  request: {
    subtitle_video_key?: string;
    voice?: string;
    model_path?: string;
    auto_p2_source?: string;
    retain_original_audio?: boolean;
    preview_only?: boolean;
    quality?: { instruct?: string; temperature?: number; top_p?: number; batch_size?: number;
      mix_mode?: string; cosy_speed?: number; cosy_mode?: string; profile_id?: string;
      debug_timing?: boolean; max_audio_speed?: number | null; auto_group_speech?: boolean; auto_rewrite_speech?: boolean };
  };
  output: {
    audio_key?: string;
    video_key?: string;
    subtitle_video_key?: string;
    duration?: number;
    segments_total?: number;
    youdub?: { task_id: string; stage: string; stages: Array<{ name: string; status: string; progress?: number }> };
    segments?: Record<string, string>;
    speech_units?: Array<{ index: number; source_indices: number[]; start: number; end: number;
      speech_text: string; slot_seconds?: number; estimated_audio_seconds?: number; estimated_overlong?: boolean }>;
    timing_issue?: { segment_index: number; duration: number; slot: number; required_speed: number } | null;
    performance?: { chunks: number; wall_seconds: number; audio_seconds: number; last_batch_size: number };
  };
  error_message: string | null;
  created_at: string;
};

export type PublishJob = {
  id: string;
  task_id: string;
  batch_id?: string | null;
  platform?: string | null;
  state: string;
  aid?: string | null;
  bvid?: string | null;
  external_id?: string | null;
  external_url?: string | null;
  account_id?: string | null;
  upload_progress?: number | null;
  upload_active?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
  tid?: number | null;
  typeid_mode?: string | null;
  typeid_selected_by?: string | null;
  typeid_ai_ok?: boolean | null;
  typeid_ai_reason?: string | null;
  auto_dub_p2?: { state?: string; title?: string; error?: string | null; dub_job_id?: string | null; cid?: number | null } | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type PublishBatch = {
  id: string;
  task_id: string;
  state: string;
  expected_targets: Array<{ key?: string; platform?: string; account_id?: string | null }>;
  outcomes: Record<string, { state?: string; detail?: string }>;
  cleanup_enqueued_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskCoreSnapshot = {
  task: Task;
  assets: Asset[];
  subtitleJobs: SubtitleJob[];
};

export type PublishActionPayload = {
  platform: "bilibili" | "douyin" | "xiaohongshu" | "kuaishou";
  account_id: string | null;
  video_key: string | null;
  video_parts?: Array<{ video: { type: "storage"; key: string }; title: string }>;
  cover_key: string | null;
  typeid_mode?: string;
  meta: Record<string, unknown>;
  platform_options: Record<string, Record<string, unknown>>;
  skip_review: boolean;
  force_retry: boolean;
};

export type VersionedPublishSettings = {
  default_meta: Record<string, unknown>;
  upload_workers?: number;
  version?: string | number | null;
};

export type YouTubeSource = {
  id: string;
  source_type: "channel" | "playlist";
  source_id: string;
  source_url: string;
  display_name?: string | null;
  license: SourceLicense;
  proof_url?: string | null;
  enabled: boolean;
  scan_interval_minutes: number;
  scan_limit: number;
  auto_process: boolean;
  last_scan_started_at?: string | null;
  last_scan_finished_at?: string | null;
  last_scan_discovered_count: number;
  last_scan_created_count: number;
  last_scan_started_pipeline_count: number;
  last_scan_skipped_duplicates: number;
  last_scan_error?: string | null;
  created_at: string;
  updated_at: string;
};
