import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type PlaylistItem = { source: "library" | "task_asset" | "live_source"; id: string };
export type PlaybackMode = "sequential" | "shuffle";
export type LiveAudioControlAction = "play" | "pause" | "resume" | "previous" | "next" | "seek" | "original" | "set_playback_mode" | "set_volume";
export type LiveSettings = {
  rtmp_url: string;
  stream_key_set: boolean;
  video_bitrate_kbps: number;
  audio_bitrate_kbps: number;
  fps: number;
  keyframe_interval_seconds: number;
};
export type LivePlaylist = {
  video_items: PlaylistItem[];
  audio_items: PlaylistItem[];
  playback_mode: PlaybackMode;
  audio_playback_mode: PlaybackMode;
  loop_playlist: boolean;
  mix_audio: boolean;
};
export type LiveMedia = {
  id: string;
  media_type: "video" | "audio";
  origin: "upload" | "completed_video" | "raw_video";
  source_task_id?: string | null;
  source_asset_id?: string | null;
  display_name: string;
  storage_key: string;
  content_type: string;
  size_bytes: number;
  sha256?: string | null;
  created_at?: string | null;
};
export type TaskVideo = {
  id: string;
  task_id: string;
  display_name: string;
  storage_key: string;
  size_bytes?: number | null;
  duration_ms?: number | null;
  created_at: string;
};
export type LiveInputSource = { id: string; url: string; display_name: string; created_at?: string | null };
export type LiveAudioPlaylist = {
  id: string;
  display_name: string;
  audio_media_ids: string[];
  created_at?: string | null;
  updated_at?: string | null;
};
export type CurrentMedia = PlaylistItem & { display_name: string };
export type LiveSession = {
  status: "idle" | "starting" | "running" | "paused" | "stopped" | "failed";
  started_at?: string | null;
  updated_at?: string | null;
  stopped_at?: string | null;
  current_video?: CurrentMedia | null;
  current_audio?: CurrentMedia | null;
  audio_player_status: "idle" | "playing" | "paused";
  audio_position_seconds: number;
  audio_duration_seconds?: number | null;
  audio_playback_mode: PlaybackMode;
  audio_volume_percent: number;
  mix_audio: boolean;
  last_error?: string | null;
};
export type LiveDashboard = {
  settings: LiveSettings;
  session: LiveSession;
  playlist: LivePlaylist;
  library_media: LiveMedia[];
  audio_playlists: LiveAudioPlaylist[];
  live_sources: LiveInputSource[];
  completed_videos: TaskVideo[];
  raw_videos: TaskVideo[];
};
export type LiveSettingsUpdate = Partial<LiveSettings> & { stream_key?: string };
export type LiveAudioControlRequest = Record<string, unknown> & { action: LiveAudioControlAction };

export const liveApi = {
  legacyStatus() {
    return fetchJson<{ enabled: boolean }>(orchestratorUrl("/live/legacy-status"));
  },
  dashboard() {
    return fetchJson<LiveDashboard>(orchestratorUrl("/live"));
  },
  updateSettings(payload: LiveSettingsUpdate) {
    return fetchJson<LiveSettings>(orchestratorUrl("/live/settings"), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  updatePlaylist(payload: LivePlaylist) {
    return fetchJson<LivePlaylist>(orchestratorUrl("/live/playlist"), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  importMedia(assetIds: string[]) {
    return fetchJson<LiveMedia[]>(orchestratorUrl("/live/media/import"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids: assetIds }),
    });
  },
  uploadMedia(type: "video" | "audio", body: FormData) {
    return fetchJson<LiveMedia[]>(orchestratorUrl(`/live/media/${type}`), { method: "POST", body });
  },
  deleteMedia(id: string) {
    return fetchJson(orchestratorUrl(`/live/media/${id}`), { method: "DELETE" });
  },
  renameMedia(id: string, displayName: string) {
    return fetchJson<LiveMedia>(orchestratorUrl(`/live/media/${id}`), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: displayName }),
    });
  },
  createAudioPlaylist(displayName: string) {
    return fetchJson<LiveAudioPlaylist>(orchestratorUrl("/live/audio-playlists"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: displayName }),
    });
  },
  deleteAudioPlaylist(id: string) {
    return fetchJson(orchestratorUrl(`/live/audio-playlists/${id}`), { method: "DELETE" });
  },
  createSource(payload: { url: string; display_name: string | null }) {
    return fetchJson<LiveInputSource>(orchestratorUrl("/live/sources"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  updateSource(id: string, payload: { url: string; display_name: string | null }) {
    return fetchJson<LiveInputSource>(orchestratorUrl(`/live/sources/${id}`), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  deleteSource(id: string) {
    return fetchJson(orchestratorUrl(`/live/sources/${id}`), { method: "DELETE" });
  },
  play(payload: { video_item: PlaylistItem; audio_item: PlaylistItem | null; mix_audio: boolean }) {
    return fetchJson<LiveSession>(orchestratorUrl("/live/actions/play"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  audioControl(payload: LiveAudioControlRequest) {
    return fetchJson<LiveSession>(orchestratorUrl("/live/actions/audio"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  action(action: "start" | "pause" | "resume" | "stop") {
    return fetchJson<LiveSession>(orchestratorUrl(`/live/actions/${action}`), { method: "POST" });
  },
  previewUrl() {
    return orchestratorUrl("/live/preview/stream.m3u8");
  },
  mediaStreamUrl(id: string) {
    return orchestratorUrl(`/live/media/${id}/stream`);
  },
};
