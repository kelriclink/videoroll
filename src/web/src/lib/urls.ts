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

/** Build an orchestrator URL without hand-rolled slash handling in pages. */
export function orchestratorUrl(path: string): string {
  const base = ORCHESTRATOR_URL.replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${suffix}`;
}

export function toWebSocketUrl(httpUrlOrPath: string, origin = "http://localhost:8000"): string {
  const httpUrl = new URL(httpUrlOrPath, origin);
  httpUrl.protocol = httpUrl.protocol === "https:" ? "wss:" : "ws:";
  return httpUrl.toString();
}

export function orchestratorWebSocketUrl(path = "/ws/events"): string {
  return toWebSocketUrl(
    orchestratorUrl(path),
    typeof window !== "undefined" ? window.location.origin : "http://localhost:8000",
  );
}
