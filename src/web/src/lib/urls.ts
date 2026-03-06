function env(name: string): string | undefined {
  const v = (import.meta as any).env?.[name];
  if (!v || typeof v !== "string") return undefined;
  return v;
}

function defaultOrchestratorUrl(): string {
  // When served behind nginx (single-port mode), use same-origin proxy.
  if (typeof window !== "undefined") return "/api";
  // Fallback (non-browser environments).
  return "http://localhost:8000";
}

export const ORCHESTRATOR_URL = env("VITE_ORCHESTRATOR_URL") ?? defaultOrchestratorUrl();
export const SUBTITLE_SERVICE_URL =
  env("VITE_SUBTITLE_SERVICE_URL") ?? `${ORCHESTRATOR_URL}/subtitle-service`;
export const YOUTUBE_INGEST_URL =
  env("VITE_YOUTUBE_INGEST_URL") ?? `${ORCHESTRATOR_URL}/youtube-ingest`;
export const BILIBILI_PUBLISHER_URL =
  env("VITE_BILIBILI_PUBLISHER_URL") ?? `${ORCHESTRATOR_URL}/bilibili-publisher`;
