import type { LiveAudioControlAction, LiveSession, PlaybackMode, PlaylistItem } from "./types";

export function itemKey(item: PlaylistItem): string {
  return `${item.source}:${item.id}`;
}

export function formatBytes(value?: number | null): string {
  if (!value || value <= 0) return "大小未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

export function formatTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

export function sessionLabel(status: LiveSession["status"]): string {
  return { idle: "未开始", starting: "启动中", running: "推流中", paused: "已暂停", stopped: "已停止", failed: "异常停止" }[status];
}

export function sessionClass(status: LiveSession["status"]): string {
  if (status === "running") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "paused" || status === "starting") return "border-amber-200 bg-amber-50 text-amber-900";
  if (status === "failed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

export function moveItem(items: PlaylistItem[], index: number, direction: -1 | 1): PlaylistItem[] {
  const nextIndex = index + direction;
  if (nextIndex < 0 || nextIndex >= items.length) return items;
  const next = [...items];
  [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
  return next;
}

export function buildLiveAudioControlPayload(args: {
  action: LiveAudioControlAction;
  audioItem: PlaylistItem | null;
  queue: PlaylistItem[];
  playbackMode: PlaybackMode;
  positionSeconds?: number;
  currentPositionSeconds?: number;
  setting?: PlaybackMode | number;
  volumePercent: number;
}): Record<string, unknown> {
  const payload: Record<string, unknown> = { action: args.action };
  if (args.action === "play" && args.audioItem) {
    payload.audio_item = args.audioItem;
    payload.audio_items = args.queue;
    payload.playback_mode = args.playbackMode;
  }
  if (args.action === "previous" || args.action === "next") {
    payload.audio_items = args.queue;
    payload.playback_mode = args.playbackMode;
  }
  if (args.action === "seek") payload.position_seconds = Math.max(0, args.positionSeconds ?? args.currentPositionSeconds ?? 0);
  if (args.action === "set_playback_mode") payload.playback_mode = args.setting ?? args.playbackMode;
  if (args.action === "set_volume") payload.volume_percent = Math.max(0, Math.min(200, Number(args.setting ?? args.volumePercent)));
  return payload;
}
