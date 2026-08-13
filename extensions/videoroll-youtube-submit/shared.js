export const DEFAULT_EXTENSION_SETTINGS = Object.freeze({
  endpoint: "",
  token: "",
  license: "authorized",
  autoPublishMode: "inherit",
});

const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{6,20}$/;
const REMOTE_PATH = "/remote/auto/youtube";

function videoIdFromPath(pathname, prefixes) {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length < 2 || !prefixes.includes(segments[0].toLowerCase())) return "";
  return segments[1];
}

function canonicalWatchUrl(videoId) {
  const value = String(videoId || "").trim();
  if (!VIDEO_ID_PATTERN.test(value)) return null;
  return `https://www.youtube.com/watch?v=${value}`;
}

export function normalizeYouTubeVideoUrl(rawValue) {
  const raw = String(rawValue || "").trim();
  if (!raw) return null;

  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;

  const hostname = url.hostname.toLowerCase().replace(/^www\./, "");
  if (hostname === "youtu.be") {
    return canonicalWatchUrl(url.pathname.split("/").filter(Boolean)[0]);
  }
  if (hostname !== "youtube.com" && !hostname.endsWith(".youtube.com")) return null;

  if (url.pathname.replace(/\/+$/, "") === "/watch") {
    return canonicalWatchUrl(url.searchParams.get("v"));
  }
  return canonicalWatchUrl(videoIdFromPath(url.pathname, ["shorts", "live", "embed", "v"]));
}

export function resolveYouTubeVideoUrl(info = {}, tabUrl = "") {
  const candidates = [info.linkUrl, info.pageUrl, tabUrl, info.srcUrl];
  for (const candidate of candidates) {
    const normalized = normalizeYouTubeVideoUrl(candidate);
    if (normalized) return normalized;
  }
  return null;
}

export function normalizeRemoteEndpoint(rawValue) {
  const raw = String(rawValue || "").trim();
  if (!raw) throw new Error("请填写 VideoRoll 地址或 Remote API 地址");

  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("VideoRoll 地址格式无效");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("VideoRoll 地址必须使用 http:// 或 https://");
  }

  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  const pathname = url.pathname.replace(/\/+$/, "");
  if (pathname.endsWith(REMOTE_PATH)) {
    url.pathname = pathname;
  } else if (!pathname) {
    url.pathname = `/api${REMOTE_PATH}`;
  } else if (pathname.endsWith("/api")) {
    url.pathname = `${pathname}${REMOTE_PATH}`;
  } else {
    throw new Error("请输入 VideoRoll 站点根地址、/api 地址或完整 Remote API 地址");
  }
  return url.toString();
}

export function endpointPermissionPattern(endpoint) {
  const url = new URL(endpoint);
  return `${url.protocol}//${url.hostname}/*`;
}

export function buildRemotePayload(videoUrl, settings) {
  const payload = {
    url: videoUrl,
    license: settings.license || DEFAULT_EXTENSION_SETTINGS.license,
  };
  if (settings.autoPublishMode === "enabled") payload.auto_publish = true;
  if (settings.autoPublishMode === "disabled") payload.auto_publish = false;
  return payload;
}

export async function responseErrorMessage(response) {
  const text = await response.text().catch(() => "");
  if (!text) return `${response.status} ${response.statusText}`.trim();
  try {
    const parsed = JSON.parse(text);
    const detail = parsed && (parsed.detail || parsed.message);
    if (typeof detail === "string" && detail.trim()) return `${response.status}: ${detail.trim()}`;
  } catch {}
  return `${response.status}: ${text.slice(0, 500)}`;
}
