import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { renderManagementApi, type CreatedEnrollment, type RenderConnection, type RenderDevice, type RenderEnrollment, type RenderExecution, type RenderWorker } from "../api/renderManagement";
import { Button, DataTable, EmptyState, PageHeader, Section } from "../components/ui";

const ACTIVE = new Set(["claimed", "running", "uploading"]);
const value = (v: unknown) => typeof v === "number" || typeof v === "string" ? String(v) : "—";
const ago = (iso?: string | null) => {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m 前`;
  return `${Math.floor(seconds / 3600)}h 前`;
};
const badge = (state: string) => {
  if (["online", "running", "busy", "succeeded"].includes(state)) return "bg-emerald-50 text-emerald-700";
  if (["failed", "lost", "canceled", "offline"].includes(state)) return "bg-rose-50 text-rose-700";
  if (["draining", "paused", "uploading"].includes(state)) return "bg-amber-50 text-amber-700";
  return "bg-slate-100 text-slate-600";
};

export default function RenderManagementPage() {
  const [workers, setWorkers] = useState<RenderWorker[]>([]);
  const [executions, setExecutions] = useState<RenderExecution[]>([]);
  const [connection, setConnection] = useState<RenderConnection | null>(null);
  const [enrollments, setEnrollments] = useState<RenderEnrollment[]>([]);
  const [createdEnrollment, setCreatedEnrollment] = useState<CreatedEnrollment | null>(null);
  const [enrollmentLabel, setEnrollmentLabel] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [selected, setSelected] = useState<RenderExecution | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextWorkers, nextExecutions, nextConnection, nextEnrollments] = await Promise.all([renderManagementApi.workers(), renderManagementApi.executions(), renderManagementApi.connection(), renderManagementApi.enrollments()]);
      setWorkers(nextWorkers); setExecutions(nextExecutions); setConnection(nextConnection); setEnrollments(nextEnrollments); setServerUrl((current) => current || nextConnection.server_url); setError(null);
      setSelected((current) => current ? nextExecutions.find((x) => x.id === current.id) ?? current : null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const online = workers.filter((w) => w.enabled && !w.stale).length;
  const active = executions.filter((e) => ACTIVE.has(e.state)).length;
  const recentFailureCutoff = Date.now() - 60 * 60 * 1000;
  const failed = executions.filter((e) => {
    if (!["failed", "lost"].includes(e.state)) return false;
    const stamp = e.finished_at ?? e.heartbeat_at ?? e.started_at;
    return stamp ? new Date(stamp).getTime() >= recentFailureCutoff : false;
  }).length;
  const schedulableWorkers = workers.filter((w) => w.enabled && !w.stale && !w.draining);
  const capacity = schedulableWorkers.reduce((n, w) => n + (w.effective_capacity ?? w.max_concurrency), 0);
  const available = schedulableWorkers.reduce(
    (n, w) => n + (w.available_slots ?? Math.max(0, (w.effective_capacity ?? w.max_concurrency) - w.active_jobs)),
    0,
  );
  const stats = useMemo(() => [
    ["在线节点", `${online} / ${workers.length}`], ["活动执行", String(active)],
    ["GPU 槽位", `${available} / ${capacity}`], ["近 1h 异常", String(failed)],
  ], [online, workers.length, active, available, capacity, failed]);

  async function workerAction(worker: RenderWorker, patch: { enabled?: boolean; draining?: boolean; max_concurrency?: number }) {
    setBusy(worker.id);
    try { await renderManagementApi.controlWorker(worker.id, patch); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); }
  }
  async function executionAction(execution: RenderExecution, action: "cancel" | "requeue") {
    setBusy(execution.id);
    try {
      if (action === "cancel") await renderManagementApi.cancelExecution(execution.id);
      else await renderManagementApi.requeueExecution(execution.id);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); }
  }
  async function createEnrollment() {
    setBusy("enrollment");
    try {
      const created = await renderManagementApi.createEnrollment({ label: enrollmentLabel.trim(), ttl_minutes: 30 });
      setCreatedEnrollment(created); setEnrollmentLabel(""); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); }
  }
  async function saveServerUrl() {
    setBusy("connection");
    try { const next = await renderManagementApi.updateConnection(serverUrl.trim()); setConnection(next); setServerUrl(next.server_url); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); }
  }

  return <div className="space-y-5">
    <PageHeader title="渲染管理" description="统一查看本机与远程渲染节点、执行状态和故障重排。任务优先级与拖拽顺序继续由处理队列管理。"
      actions={<><Link to="/queue/render" className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50">处理队列</Link><Button onClick={() => void refresh()}>刷新</Button></>} />
    {error ? <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

    <Section>
      <div className="text-sm font-semibold">添加渲染节点</div>
      <div className="mt-1 text-xs text-slate-500">在渲染机上填写服务器地址和一次性配对凭据。配对成功后渲染机会换取并仅在本机保存独立长期凭据。</div>
      <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
        <label><div className="mb-1 text-xs text-slate-500">服务器地址</div><input value={serverUrl} onChange={(e) => setServerUrl(e.target.value)} placeholder="https://video.example.com" className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" /></label>
        <div className="flex items-end"><Button disabled={busy === "connection" || !serverUrl.trim()} onClick={() => void saveServerUrl()}>保存地址</Button></div>
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px_auto]">
        <div className="self-end pb-2 text-xs text-slate-500">Worker API：{connection ? `${connection.server_url}${connection.worker_api_path}` : "—"}</div>
        <label><div className="mb-1 text-xs text-slate-500">节点备注</div><input value={enrollmentLabel} onChange={(e) => setEnrollmentLabel(e.target.value)} placeholder="例如：4060 笔记本" className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" /></label>
        <div className="flex items-end"><Button tone="primary" disabled={busy === "enrollment"} onClick={() => void createEnrollment()}>生成配对凭据</Button></div>
      </div>
      {createdEnrollment ? <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
        <div className="text-sm font-medium text-amber-900">一次性配对凭据（仅显示本次生成结果）</div>
        <div className="mt-2 break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900">{createdEnrollment.token}</div>
        <div className="mt-2 text-xs text-amber-800">有效期 30 分钟，成功配对后立即失效。不要把它作为长期 Worker Token 保存。</div>
      </div> : null}
      {enrollments.length ? <div className="mt-4 text-xs text-slate-500">最近配对凭据：{enrollments.slice(0, 5).map((x) => `${x.label || x.id.slice(0, 8)} · ${x.status}`).join("　")}</div> : null}
    </Section>

    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map(([label, n]) => <Section key={label}><div className="text-xs text-slate-500">{label}</div><div className="mt-2 text-2xl font-semibold">{n}</div></Section>)}
    </div>

    <Section>
      <div className="mb-4"><div className="text-sm font-semibold">渲染节点</div><div className="mt-1 text-xs text-slate-500">节点超过 180 秒无心跳标记为失联；Drain 只阻止领取新任务。</div></div>
      {workers.length === 0 ? <EmptyState>还没有可用的渲染节点。</EmptyState> :
      <div className="grid gap-3 lg:grid-cols-2">
        {workers.map((w) => {
          const gpu = (w.capabilities.gpu_model ?? w.capabilities.gpu ?? w.resources.gpu) as unknown;
          const fps = w.resources.fps ?? w.resources.render_fps;
          const devices = (Array.isArray(w.resources.devices) ? w.resources.devices : Array.isArray(w.capabilities.devices) ? w.capabilities.devices : []) as RenderDevice[];
          return <div key={w.id} className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
            <div className="flex items-start justify-between gap-3"><div><div className="font-medium">{w.name}</div><div className="mt-1 text-xs text-slate-500">{w.platform}{w.architecture ? ` · ${w.architecture}` : ""} · {value(gpu)}</div></div>
              <span className={`rounded-full px-2 py-1 text-xs ${badge(w.stale ? "offline" : w.status)}`}>{w.stale ? "失联" : w.draining ? "Drain" : w.status}</span></div>
            <div className="mt-4 grid grid-cols-5 gap-3 text-xs"><div><div className="text-slate-500">运行中</div><div className="mt-1 font-medium">{w.active_jobs}</div></div><label><div className="text-slate-500">管理上限</div><input type="number" min={1} max={32} defaultValue={w.max_concurrency} disabled={busy === w.id} onBlur={(e) => { const n = Math.max(1, Math.min(32, Number(e.currentTarget.value) || 1)); if (n !== w.max_concurrency) void workerAction(w, { max_concurrency: n }); }} className="mt-1 w-16 rounded border border-slate-300 px-2 py-1 font-medium" /></label><div><div className="text-slate-500">有效槽位</div><div className="mt-1 font-medium">{w.available_slots ?? 0} / {w.effective_capacity ?? w.max_concurrency}</div></div><div><div className="text-slate-500">速度</div><div className="mt-1 font-medium">{value(fps)} FPS</div></div><div><div className="text-slate-500">心跳</div><div className="mt-1 font-medium">{ago(w.last_seen_at)}</div></div></div>
            {devices.length ? <div className="mt-4 space-y-2">
              {devices.map((device) => <div key={device.id} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
                <div className="flex items-center justify-between gap-3"><div><div className="text-xs font-medium">{device.name}</div><div className="mt-0.5 font-mono text-[11px] text-slate-500">{device.id}{device.path ? ` · ${device.path}` : ""} · {device.backend}</div></div><span className={`rounded-full px-2 py-0.5 text-[11px] ${badge(device.status)}`}>{device.status}</span></div>
                <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]"><div><span className="text-slate-500">任务 </span>{device.active_jobs}/{device.max_concurrency}</div><div><span className="text-slate-500">空闲槽位 </span>{device.available_slots}</div><div className="truncate"><span className="text-slate-500">Execution </span>{device.execution_ids?.length ? device.execution_ids.map((id) => id.slice(0, 8)).join(", ") : "—"}</div></div>
              </div>)}
            </div> : null}
            <div className="mt-4 flex flex-wrap gap-2"><Button size="xs" disabled={busy === w.id} onClick={() => void workerAction(w, { draining: !w.draining })}>{w.draining ? "恢复接单" : "Drain"}</Button><Button size="xs" tone={w.enabled ? "danger" : "primary"} disabled={busy === w.id} onClick={() => void workerAction(w, { enabled: !w.enabled })}>{w.enabled ? "禁用" : "启用"}</Button><Button size="xs" tone="danger" disabled={busy === w.id || !w.credential_active} onClick={async () => { setBusy(w.id); try { await renderManagementApi.revokeWorkerCredential(w.id); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(null); } }}>吊销凭据</Button></div>
          </div>;
        })}
      </div>}
    </Section>

    <Section>
      <div className="mb-4"><div className="text-sm font-semibold">执行记录</div><div className="mt-1 text-xs text-slate-500">每次 Worker 领取任务都会产生独立 Attempt；重新排队会生成新的 fence token。</div></div>
      {executions.length === 0 ? <EmptyState>暂无远程渲染执行记录。</EmptyState> :
      <DataTable><thead><tr><th>状态</th><th>节点</th><th>进度</th><th>Attempt</th><th>Task</th><th>开始</th><th>操作</th></tr></thead><tbody>
        {executions.map((e) => <tr key={e.id} className="cursor-pointer" onClick={() => setSelected(e)}>
          <td><span className={`rounded-full px-2 py-1 text-xs ${badge(e.state)}`}>{e.state}</span></td><td><div>{e.worker_name ?? e.worker_id.slice(0, 8)}</div>{typeof e.metrics.device_name === "string" ? <div className="mt-0.5 text-[11px] text-slate-500">{e.metrics.device_name}</div> : null}</td><td>{e.progress}%</td><td>#{e.attempt}</td><td>{e.task_id ? <Link className="text-sky-700 hover:underline" to={`/tasks/${e.task_id}`} onClick={(x) => x.stopPropagation()}>{e.task_id.slice(0, 8)}</Link> : "—"}</td><td>{ago(e.started_at)}</td>
          <td><div className="flex gap-1" onClick={(x) => x.stopPropagation()}>{ACTIVE.has(e.state) ? <Button size="xs" tone="danger" disabled={busy === e.id} onClick={() => void executionAction(e, "cancel")}>取消</Button> : null}<Button size="xs" disabled={busy === e.id || e.state === "succeeded"} onClick={() => void executionAction(e, "requeue")}>重排</Button></div></td>
        </tr>)}
      </tbody></DataTable>}
    </Section>

    {selected ? <Section>
      <div className="flex items-start justify-between gap-3"><div><div className="text-sm font-semibold">执行详情 · Attempt #{selected.attempt}</div><div className="mt-1 font-mono text-xs text-slate-500">{selected.id}</div></div><Button size="xs" onClick={() => setSelected(null)}>关闭</Button></div>
      <div className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4"><div><span className="text-slate-500">Worker：</span>{selected.worker_name ?? selected.worker_id}</div><div><span className="text-slate-500">状态：</span>{selected.state}</div><div><span className="text-slate-500">Job：</span>{selected.job_status ?? "—"}</div><div><span className="text-slate-500">Lease：</span>{ago(selected.lease_until)}</div></div>
      {selected.error_message ? <div className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-700">{selected.error_message}</div> : null}
      <div className="mt-4 grid gap-4 lg:grid-cols-2"><div><div className="mb-2 text-xs font-medium text-slate-500">Metrics</div><pre className="max-h-52 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">{JSON.stringify(selected.metrics, null, 2)}</pre></div><div><div className="mb-2 text-xs font-medium text-slate-500">日志尾部</div><pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-xs text-slate-100">{selected.log_tail || "暂无日志"}</pre></div></div>
    </Section> : null}
  </div>;
}

