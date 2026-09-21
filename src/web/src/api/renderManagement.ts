import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type RenderDevice = {
  id: string; name: string; backend: string; path?: string; index?: number | null;
  encoders?: string[]; max_concurrency?: number; active_jobs: number; available_slots?: number;
  status: string; execution_ids?: string[];
};

export type RenderWorker = {
  id: string; worker_key: string; name: string; platform: string; architecture?: string | null;
  version: string; protocol_version: number; render_spec_versions: number[];
  capabilities: Record<string, unknown>; resources: Record<string, unknown>; labels: Record<string, unknown>;
  status: string; enabled: boolean; draining: boolean; max_concurrency: number; active_jobs: number;
  device_count?: number; detected_capacity?: number; effective_capacity?: number; available_slots?: number;
  last_seen_at: string; stale: boolean; seconds_since_heartbeat: number; credential_active: boolean;
};

export type RenderConnection = { server_url: string; worker_api_path: string; protocol_version: number };
export type RenderEnrollment = { id: string; label: string; status: string; expires_at: string; consumed_at?: string | null; worker_id?: string | null; created_at: string };
export type CreatedEnrollment = { id: string; token: string; server_url: string; expires_at: string };

export type RenderExecution = {
  id: string; render_job_id: string; worker_id: string; attempt: number;
  state: string; transfer_mode: string; progress: number; lease_until?: string | null;
  render_spec: Record<string, unknown>; worker_name?: string | null; task_id?: string | null;
  job_status?: string | null; metrics: Record<string, unknown>; log_tail?: string | null;
  error_message?: string | null; heartbeat_at?: string | null; started_at: string; finished_at?: string | null;
};

const json = (body: unknown) => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const renderManagementApi = {
  connection: () => fetchJson<RenderConnection>(orchestratorUrl("/render-management/connection")),
  updateConnection: (server_url: string) => fetchJson<RenderConnection>(orchestratorUrl("/render-management/connection"), { method: "PUT", ...json({ server_url }) }),
  enrollments: () => fetchJson<RenderEnrollment[]>(orchestratorUrl("/render-management/enrollments")),
  createEnrollment: (payload: { label: string; ttl_minutes: number }) =>
    fetchJson<CreatedEnrollment>(orchestratorUrl("/render-management/enrollments"), { method: "POST", ...json(payload) }),
  revokeEnrollment: (id: string) =>
    fetchJson<RenderEnrollment>(orchestratorUrl(`/render-management/enrollments/${id}`), { method: "DELETE" }),
  workers: () => fetchJson<RenderWorker[]>(orchestratorUrl("/render-management/workers")),
  executions: (limit = 100) => fetchJson<RenderExecution[]>(orchestratorUrl(`/render-management/executions?limit=${limit}`)),
  controlWorker: (id: string, payload: { enabled?: boolean; draining?: boolean; max_concurrency?: number; worker_key?: string }) =>
    fetchJson<RenderWorker>(orchestratorUrl(`/render-management/workers/${id}`), { method: "PATCH", ...json(payload) }),
  revokeWorkerCredential: (id: string) =>
    fetchJson<RenderWorker>(orchestratorUrl(`/render-management/workers/${id}/revoke-credential`), { method: "POST" }),
  cancelExecution: (id: string, reason = "管理员取消") =>
    fetchJson<RenderExecution>(orchestratorUrl(`/render-management/executions/${id}/cancel`), { method: "POST", ...json({ reason }) }),
  requeueExecution: (id: string, reason = "管理员重新排队") =>
    fetchJson<RenderExecution>(orchestratorUrl(`/render-management/executions/${id}/requeue`), { method: "POST", ...json({ reason }) }),
};

