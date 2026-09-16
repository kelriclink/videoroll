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

export function ffplayoutUrlForLocation(
  location: Pick<Location, "protocol" | "hostname">,
  port = "3003",
): string {
  const hostname = location.hostname;
  const host = hostname.includes(":") ? `[${hostname}]` : hostname;
  return `${location.protocol}//${host}:${port}`;
}

function defaultFfplayoutUrl(): string {
  if (typeof window === "undefined") return "";

  const configuredPort = env("VITE_FFPLAYOUT_PORT")?.trim() || "3003";
  return ffplayoutUrlForLocation(window.location, configuredPort);
}

/**
 * Public browser origin for ffplayout.
 *
 * A fully-qualified VITE_FFPLAYOUT_URL can still override the default for
 * unusual reverse-proxy topologies. Otherwise ffplayout follows the hostname
 * used to open VideoRoll and only changes the port, so one Web image works via
 * LAN IPs, VPN addresses, and alternate DNS names.
 */
export const FFPLAYOUT_URL = env("VITE_FFPLAYOUT_URL") ?? defaultFfplayoutUrl();

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
