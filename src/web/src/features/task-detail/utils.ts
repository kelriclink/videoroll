import type { BiliTypeNode, DesktopGrant, YouTubeDownloadProgress, YouTubeSubtitleMode } from "./types";

export function clampUploadProgress(value: number | null | undefined): number {
  const progress = Number(value);
  if (!Number.isFinite(progress)) return 0;
  return Math.max(0, Math.min(100, Math.floor(progress)));
}

export function formatDownloadBytes(value?: number | null): string {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

export function youtubeDownloadStatusLabel(status: YouTubeDownloadProgress["status"]): string {
  return {
    idle: "等待下载",
    preparing: "正在准备",
    downloading: "正在下载",
    processing: "正在合并与校验",
    uploading: "正在保存到媒体库",
    completed: "下载完成",
    failed: "下载失败",
  }[status];
}

export function normalizeYouTubeSubtitleMode(value: unknown, legacyPrefer?: boolean | null): YouTubeSubtitleMode {
  const mode = String(value ?? "").trim().toLowerCase();
  if (mode === "off" || mode === "target" || mode === "auto_source") return mode;
  if (legacyPrefer === false) return "off";
  return "target";
}

export function clampText(text: string, maxLen: number): string {
  const s = (text ?? "").trim();
  if (s.length <= maxLen) return s;
  if (maxLen <= 1) return s.slice(0, maxLen);
  return s.slice(0, maxLen - 1) + "…";
}

export function upsertById<T extends { id: string }>(current: T[] | null, item: T): T[] {
  const rows = current ?? [];
  const index = rows.findIndex((row) => row.id === item.id);
  if (index < 0) return [...rows, item];
  const next = [...rows];
  next[index] = { ...rows[index], ...item };
  return next;
}

export function removeById<T extends { id: string }>(current: T[] | null, id: string): T[] | null {
  return current ? current.filter((row) => row.id !== id) : current;
}

export function scopedDesktopUrl(browserUrl: string, grant: DesktopGrant): string {
  const url = new URL(browserUrl, window.location.origin);
  const noVncPath = url.searchParams.get("path");
  if (!noVncPath) throw new Error("noVNC desktop path is missing");
  const separator = noVncPath.includes("?") ? "&" : "?";
  url.searchParams.set("grant", grant.token);
  url.searchParams.set("resource", grant.resource_id);
  url.searchParams.set("path", `${noVncPath}${separator}grant=${grant.token}&resource=${grant.resource_id}`);
  return url.toString();
}

export function flattenBiliTypeOptions(nodes: BiliTypeNode[] | null): Array<{ id: number; label: string }> {
  const out: Array<{ id: number; label: string }> = [];
  const walk = (node: BiliTypeNode, parents: string[]) => {
    const name = String(node?.name ?? "").trim();
    const next = name ? [...parents, name] : parents;
    const children = Array.isArray(node?.children) ? node.children : [];
    if (children.length) {
      children.forEach((child) => walk(child, next));
      return;
    }
    const id = Number(node?.id ?? 0);
    if (!Number.isFinite(id) || id <= 0) return;
    out.push({ id, label: next.filter(Boolean).join(" / ") || String(id) });
  };
  (nodes ?? []).forEach((node) => walk(node, []));
  return out;
}

export function selectTaskAssets(assets: import("../../lib/types").Asset[] | null) {
  const rows = assets ?? [];
  const rawAssets = rows.filter((asset) => asset.kind === "video_raw");
  const finalAssets = rows.filter((asset) => asset.kind === "video_final");
  const subtitleAssets = rows.filter((asset) => asset.kind === "subtitle_srt" || asset.kind === "subtitle_ass");
  const coverAssets = rows.filter((asset) => asset.kind === "cover_image");
  const rawAsset = rawAssets.length ? rawAssets[rawAssets.length - 1] : null;
  const metadata = rows.filter((asset) => asset.kind === "metadata_json");
  const metadataAsset = metadata.length ? metadata[metadata.length - 1] : null;
  return { rawAssets, rawAsset, metadataAsset, finalAssets, subtitleAssets, coverAssets };
}
