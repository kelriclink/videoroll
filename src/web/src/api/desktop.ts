import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type DesktopGrant = {
  token: string;
  desktop_type: "login" | "publish";
  resource_id: string;
  expires_at: string;
  reconnect_limit: number;
};

export type DesktopGrantPayload = {
  desktop_type: "login" | "publish";
  resource_id: string;
};

export const desktopApi = {
  createGrant(payload: DesktopGrantPayload) {
    return fetchJson<DesktopGrant>(orchestratorUrl("/desktop/grants"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
};
