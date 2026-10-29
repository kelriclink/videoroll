export type YouTubeSubtitleMode = "off" | "target" | "auto_source";

export function normalizeYouTubeSubtitleMode(
  value: unknown,
  legacyPrefer?: boolean | null,
): YouTubeSubtitleMode {
  const mode = String(value ?? "").trim().toLowerCase();
  if (mode === "off" || mode === "target" || mode === "auto_source") return mode;
  if (legacyPrefer === false) return "off";
  return "target";
}
