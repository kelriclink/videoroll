import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  renderManagementApi,
  type CreatedEnrollment,
  type RenderConnection,
  type RenderDevice,
  type RenderExecution,
  type RenderWorker,
} from "../api/renderManagement";
import { useConfirm } from "../components/feedbackContext";
import { Button, EmptyState, InlineNotice, Metric, PageHeader, Section, TechnicalDetails } from "../components/ui";
import { RenderEnrollmentPanel } from "../features/render-management/RenderEnrollmentPanel";
import { RenderExecutions } from "../features/render-management/RenderExecutions";
import { RenderWorkerCard } from "../features/render-management/RenderWorkerCard";
import { ACTIVE_RENDER_STATES, type WorkerDraft } from "../features/render-management/presentation";

export default function RenderManagementPage() {
  const confirm = useConfirm();
  const [workers, setWorkers] = useState<RenderWorker[]>([]);
  const [executions, setExecutions] = useState<RenderExecution[]>([]);
  const [connection, setConnection] = useState<RenderConnection | null>(null);
  const [createdEnrollment, setCreatedEnrollment] = useState<CreatedEnrollment | null>(null);
  const [enrollmentLabel, setEnrollmentLabel] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [selected, setSelected] = useState<RenderExecution | null>(null);
  const [drafts, setDrafts] = useState<Record<string, WorkerDraft>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextWorkers, nextExecutions, nextConnection] = await Promise.all([
        renderManagementApi.workers(),
        renderManagementApi.executions(),
        renderManagementApi.connection(),
      ]);
      setWorkers(nextWorkers);
      setExecutions(nextExecutions);
      setConnection(nextConnection);
      setServerUrl((current) => current || nextConnection.server_url);
      setDrafts((current) => {
        const next = { ...current };
        for (const worker of nextWorkers) {
          const existing = next[worker.id];
          const unchanged = !existing
            || (existing.maxConcurrency === worker.max_concurrency && existing.workerKey === worker.worker_key);
          if (unchanged) {
            next[worker.id] = {
              maxConcurrency: worker.max_concurrency,
              workerKey: worker.worker_key,
            };
          }
        }
        return next;
      });
      setSelected((current) =>
        current ? nextExecutions.find((item) => item.id === current.id) ?? current : null
      );
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const online = workers.filter((worker) => worker.enabled && !worker.stale).length;
  const activeCount = executions.filter((execution) => ACTIVE_RENDER_STATES.has(execution.state)).length;
  const failureCutoff = Date.now() - 60 * 60 * 1000;
  const recentFailures = executions.filter((execution) => {
    if (!["failed", "lost"].includes(execution.state)) return false;
    const stamp = execution.finished_at ?? execution.heartbeat_at ?? execution.started_at;
    return stamp ? new Date(stamp).getTime() >= failureCutoff : false;
  }).length;
  const schedulable = workers.filter((worker) => worker.enabled && !worker.stale && !worker.draining);
  const capacity = schedulable.reduce((sum, worker) => sum + worker.max_concurrency, 0);
  const available = schedulable.reduce(
    (sum, worker) => sum + (worker.available_slots ?? Math.max(0, worker.max_concurrency - worker.active_jobs)),
    0,
  );

  async function workerAction(
    worker: RenderWorker,
    patch: { enabled?: boolean; draining?: boolean; max_concurrency?: number; worker_key?: string },
  ) {
    setBusy(worker.id);
    try {
      await renderManagementApi.controlWorker(worker.id, patch);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  function updateDraft(worker: RenderWorker, patch: Partial<WorkerDraft>) {
    setDrafts((current) => ({
      ...current,
      [worker.id]: {
        maxConcurrency: current[worker.id]?.maxConcurrency ?? worker.max_concurrency,
        workerKey: current[worker.id]?.workerKey ?? worker.worker_key,
        ...patch,
      },
    }));
  }

  function resetDraft(worker: RenderWorker) {
    setDrafts((current) => ({
      ...current,
      [worker.id]: {
        maxConcurrency: worker.max_concurrency,
        workerKey: worker.worker_key,
      },
    }));
  }

  async function saveWorkerSettings(worker: RenderWorker) {
    const draft = drafts[worker.id];
    if (!draft) return;
    const maxConcurrency = Math.max(1, Math.min(32, Number(draft.maxConcurrency) || 1));
    const workerKey = draft.workerKey.trim();
    if (!workerKey) {
      setError("节点身份 ID 不能为空。");
      return;
    }

    const patch: { max_concurrency?: number; worker_key?: string } = {};
    if (maxConcurrency !== worker.max_concurrency) patch.max_concurrency = maxConcurrency;
    if (workerKey !== worker.worker_key) patch.worker_key = workerKey;
    if (!Object.keys(patch).length) return;
    await workerAction(worker, patch);
  }

  async function deviceAction(worker: RenderWorker, device: RenderDevice, enabled: boolean) {
    const key = `device:${worker.id}:${device.id}`;
    setBusy(key);
    try {
      await renderManagementApi.controlDevice(worker.id, device.id, enabled);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function executionAction(execution: RenderExecution, action: "cancel" | "requeue") {
    const ok = await confirm({
      title: action === "cancel" ? "取消本次渲染执行" : "重新执行渲染",
      message: action === "cancel"
        ? "当前执行会被取消；任务本身仍保留，可以之后重新执行。"
        : "会把该渲染任务重新加入队列，并创建一次新的执行记录。",
      confirmLabel: action === "cancel" ? "取消执行" : "重新执行",
      tone: action === "cancel" ? "danger" : "warning",
    });
    if (!ok) return;

    setBusy(execution.id);
    try {
      if (action === "cancel") await renderManagementApi.cancelExecution(execution.id);
      else await renderManagementApi.requeueExecution(execution.id);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function createEnrollment(worker?: RenderWorker) {
    setBusy(worker ? `recover:${worker.id}` : "enrollment");
    try {
      const label = worker
        ? `恢复 ${worker.name} · ${worker.worker_key}`
        : enrollmentLabel.trim();
      const created = await renderManagementApi.createEnrollment({ label, ttl_minutes: 30 });
      setCreatedEnrollment(created);
      if (!worker) setEnrollmentLabel("");
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function revokeCredential(worker: RenderWorker) {
    const ok = await confirm({
      title: "吊销节点凭据",
      message: "该节点现有长期凭据会立即失效。之后需要新的配对凭据才能重新接入。",
      confirmLabel: "吊销凭据",
      tone: "danger",
    });
    if (!ok) return;

    setBusy(worker.id);
    try {
      await renderManagementApi.revokeWorkerCredential(worker.id);
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function saveServerUrl() {
    setBusy("connection");
    try {
      const next = await renderManagementApi.updateConnection(serverUrl.trim());
      setConnection(next);
      setServerUrl(next.server_url);
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="渲染节点"
        description="管理渲染容量、当前任务和 GPU 分配。任务顺序与优先级仍在处理队列中管理。"
        actions={
          <>
            <Link
              to="/queue/render"
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-950 dark:hover:bg-slate-900"
            >
              处理队列
            </Link>
            <Button onClick={() => void refresh()}>刷新</Button>
          </>
        }
      />

      {error ? (
        <InlineNotice title="渲染管理操作未完成" tone="danger">
          页面保留了最后一次成功读取的状态。
          <TechnicalDetails className="mt-2">
            <pre className="whitespace-pre-wrap break-words font-mono">{error}</pre>
          </TechnicalDetails>
        </InlineNotice>
      ) : null}

      <Section>
        <div className="grid grid-cols-2 gap-5 lg:grid-cols-4">
          <Metric label="在线节点" value={`${online} / ${workers.length}`} tone={online ? "success" : "warning"} />
          <Metric label="正在执行" value={activeCount} tone={activeCount ? "info" : "neutral"} />
          <Metric label="可用并发" value={`${available} / ${capacity}`} detail="仅统计可接收新任务的节点" />
          <Metric label="近 1 小时异常" value={recentFailures} tone={recentFailures ? "danger" : "success"} />
        </div>
      </Section>

      <Section>
        <div className="mb-4">
          <div className="text-sm font-semibold text-slate-950 dark:text-white">节点</div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            “停止接收新任务”不会中断当前渲染；关闭节点会阻止它领取后续任务。
          </div>
        </div>

        {workers.length === 0 ? <EmptyState>还没有可用的渲染节点。</EmptyState> : (
          <div className="grid gap-4 xl:grid-cols-2">
            {workers.map((worker) => (
              <RenderWorkerCard
                key={worker.id}
                worker={worker}
                executions={executions.filter(
                  (execution) =>
                    execution.worker_id === worker.id
                    && ACTIVE_RENDER_STATES.has(execution.state)
                )}
                draft={drafts[worker.id] ?? {
                  maxConcurrency: worker.max_concurrency,
                  workerKey: worker.worker_key,
                }}
                busy={busy}
                onWorkerAction={(item, patch) => void workerAction(item, patch)}
                onDraftChange={updateDraft}
                onSaveSettings={(item) => void saveWorkerSettings(item)}
                onResetDraft={resetDraft}
                onDeviceAction={(item, device, enabled) => void deviceAction(item, device, enabled)}
                onCreateEnrollment={(item) => void createEnrollment(item)}
                onRevokeCredential={(item) => void revokeCredential(item)}
              />
            ))}
          </div>
        )}
      </Section>

      <RenderExecutions
        executions={executions}
        selected={selected}
        busy={busy}
        onSelect={setSelected}
        onClose={() => setSelected(null)}
        onAction={(execution, action) => void executionAction(execution, action)}
      />

      <RenderEnrollmentPanel
        connection={connection}
        createdEnrollment={createdEnrollment}
        enrollmentLabel={enrollmentLabel}
        serverUrl={serverUrl}
        busy={busy}
        onEnrollmentLabelChange={setEnrollmentLabel}
        onServerUrlChange={setServerUrl}
        onSaveServerUrl={() => void saveServerUrl()}
        onCreateEnrollment={() => void createEnrollment()}
      />
    </div>
  );
}
