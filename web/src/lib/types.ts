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
  priority: number;
  created_by?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
};

export type Asset = {
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

export type PublishJob = {
  id: string;
  task_id: string;
  state: string;
  aid?: string | null;
  bvid?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type YouTubeSource = {
  id: string;
  source_type: "channel" | "playlist";
  source_id: string;
  license: SourceLicense;
  proof_url?: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};
