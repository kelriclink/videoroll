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

function defaultFfplayoutUrl(): string {
  // ffplayout is reverse-proxied by the Web nginx on the same browser origin.
  return "/playout/";
}

/**
 * Browser URL for ffplayout.
 *
 * The default same-origin /playout/ path works through LAN IPs, VPN addresses,
 * alternate DNS names, and an outer TLS reverse proxy without exposing a
 * second browser port. VITE_FFPLAYOUT_URL remains available for unusual
 * deployments that intentionally host ffplayout elsewhere.
 */
export const FFPLAYOUT_URL = env("VITE_FFPLAYOUT_URL") ?? defaultFfplayoutUrl();

function defaultHatchetDashboardUrl(): string {
  // Hatchet Lite is reverse-proxied by the Web nginx on the same browser
  // origin. v0.107.0 serves this subpath natively via
  // LITE_FRONTEND_BASE_PATH, so no extra browser-facing port is required.
  return "/workflow-ui/";
}

/** Browser URL for the native Hatchet observability dashboard. */
export const HATCHET_DASHBOARD_URL = env("VITE_HATCHET_DASHBOARD_URL") ?? defaultHatchetDashboardUrl();

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
