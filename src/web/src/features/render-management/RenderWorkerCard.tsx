import { Link } from "react-router-dom";
import type { RenderDevice, RenderExecution, RenderWorker } from "../../api/renderManagement";
import { Button, TechnicalDetails } from "../../components/ui";
import {
  formatRenderDuration,
  formatRenderFps,
  formatRenderSpeed,
  getRenderTelemetry,
  summarizeRenderExecutions,
} from "../renderTelemetry";
import {
  ago,
  badgeClass,
  deviceStatusLabel,
  devicesForWorker,
  executionStatusLabel,
  platformLabel,
  workerStatusLabel,
  type WorkerDraft,
} from "./presentation";

export function RenderWorkerCard({
  worker,
  executions,
  draft,
  busy,
  onWorkerAction,
  onDraftChange,
  onSaveSettings,
  onResetDraft,
  onDeviceAction,
  onCreateEnrollment,
  onRevokeCredential,
}: {
  worker: RenderWorker;
  executions: RenderExecution[];
  draft: WorkerDraft;
  busy: string | null;
  onWorkerAction: (
    worker: RenderWorker,
    patch: { enabled?: boolean; draining?: boolean; max_concurrency?: number; worker_key?: string },
  ) => void;
  onDraftChange: (worker: RenderWorker, patch: Partial<WorkerDraft>) => void;
  onSaveSettings: (worker: RenderWorker) => void;
  onResetDraft: (worker: RenderWorker) => void;
  onDeviceAction: (worker: RenderWorker, device: RenderDevice, enabled: boolean) => void;
  onCreateEnrollment: (worker: RenderWorker) => void;
  onRevokeCredential: (worker: RenderWorker) => void;
}) {
  const throughput = summarizeRenderExecutions(executions);
  const devices = devicesForWorker(worker);
  const dirty =
    draft.maxConcurrency !== worker.max_concurrency
    || draft.workerKey.trim() !== worker.worker_key;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-medium text-slate-950 dark:text-white">{worker.name}</div>
          <div className="mt-1 truncate text-xs text-slate-500">{platformLabel(worker) || "渲染节点"}</div>
        </div>
        <span className={`rounded-full px-2 py-1 text-xs ${badgeClass(worker.stale ? "offline" : worker.draining ? "draining" : worker.status)}`}>
          {workerStatusLabel(worker)}
        </span>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-4 text-sm">
        <div>
          <div className="text-xs text-slate-500">运行中</div>
          <div className="mt-1 font-medium">{worker.active_jobs}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">可用并发</div>
          <div className="mt-1 font-medium">
            {worker.available_slots ?? Math.max(0, worker.max_concurrency - worker.active_jobs)} / {worker.max_concurrency}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">最后心跳</div>
          <div className="mt-1 font-medium">{ago(worker.last_seen_at)}</div>
        </div>
      </div>

      {executions.length > 0 ? (
        <div className="mt-4 space-y-2">
          <div className="text-xs font-medium text-slate-500">当前任务</div>
          {executions.map((execution) => {
            const telemetry = getRenderTelemetry(execution);
            return (
              <div key={execution.id} className="rounded-lg border border-sky-200 bg-sky-50/60 p-3 dark:border-sky-900 dark:bg-sky-950/20">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                  <div className="min-w-0">
                    {execution.task_id ? (
                      <Link
                        className="font-medium text-sky-700 hover:underline dark:text-sky-300"
                        to={`/tasks/${execution.task_id}`}
                      >
                        任务 {execution.task_id.slice(0, 8)}
                      </Link>
                    ) : <span className="font-medium">渲染执行</span>}
                    <span className="ml-2 text-slate-500">{executionStatusLabel(execution.state)}</span>
                  </div>
                  <div className="flex shrink-0 gap-3 font-mono">
                    <span>{formatRenderFps(telemetry.fps)}</span>
                    <span>{formatRenderSpeed(telemetry.speed)}</span>
                  </div>
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                  <div className="h-full rounded-full bg-sky-500" style={{ width: `${telemetry.overallProgress}%` }} />
                </div>
                <div className="mt-1 flex justify-between text-[11px] text-slate-500">
                  <span>{telemetry.overallProgress}%</span>
                  <span>预计 {formatRenderDuration(telemetry.etaSeconds)}</span>
                </div>
              </div>
            );
          })}
          {throughput.fps > 0 || throughput.speed > 0 ? (
            <div className="text-[11px] text-slate-500">
              节点合计：{formatRenderFps(throughput.fps)} · {formatRenderSpeed(throughput.speed)}
            </div>
          ) : null}
        </div>
      ) : null}

      {devices.length > 0 ? (
        <div className="mt-4 space-y-2">
          <div className="text-xs font-medium text-slate-500">GPU 分配</div>
          {devices.map((device) => (
            <div key={device.id} className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
              <div className="min-w-0">
                <div className="truncate text-xs font-medium">{device.name}</div>
                <div className="mt-0.5 text-[11px] text-slate-500">
                  {deviceStatusLabel(device)} · 当前任务 {device.active_jobs}
                </div>
              </div>
              <Button
                size="xs"
                tone={device.enabled === false ? "primary" : "secondary"}
                disabled={busy !== null || worker.stale || !worker.credential_active}
                onClick={() => onDeviceAction(worker, device, device.enabled === false)}
              >
                {device.enabled === false ? "允许分配新任务" : "停止分配新任务"}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          size="xs"
          disabled={busy === worker.id || !worker.enabled}
          onClick={() => onWorkerAction(worker, { draining: !worker.draining })}
        >
          {worker.draining ? "恢复接收新任务" : "停止接收新任务"}
        </Button>
        <Button
          size="xs"
          tone={worker.enabled ? "danger" : "primary"}
          disabled={busy === worker.id}
          onClick={() => onWorkerAction(worker, { enabled: !worker.enabled })}
        >
          {worker.enabled ? "关闭节点" : "启用节点"}
        </Button>
      </div>

      <TechnicalDetails className="mt-4" summary="节点设置与恢复">
        <div className="grid gap-3 sm:grid-cols-2">
          <label>
            <div className="mb-1 text-xs text-slate-500">最大并发</div>
            <input
              className="vr-input w-full"
              type="number"
              min={1}
              max={32}
              value={draft.maxConcurrency}
              onChange={(event) => onDraftChange(worker, { maxConcurrency: Number(event.target.value) })}
            />
          </label>
          <label>
            <div className="mb-1 text-xs text-slate-500">节点身份 ID</div>
            <input
              className="vr-input w-full font-mono"
              value={draft.workerKey}
              onChange={(event) => onDraftChange(worker, { workerKey: event.target.value })}
            />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="xs" tone="primary" disabled={!dirty || busy === worker.id} onClick={() => onSaveSettings(worker)}>
            保存节点设置
          </Button>
          <Button size="xs" disabled={!dirty || busy === worker.id} onClick={() => onResetDraft(worker)}>
            放弃修改
          </Button>
          <Button size="xs" disabled={busy === `recover:${worker.id}`} onClick={() => onCreateEnrollment(worker)}>
            生成恢复凭据
          </Button>
          <Button
            size="xs"
            tone="danger"
            disabled={busy === worker.id || !worker.credential_active}
            onClick={() => onRevokeCredential(worker)}
          >
            吊销节点凭据
          </Button>
        </div>
        <div className="mt-2 text-[11px] text-slate-500">
          修改节点身份 ID 只应在恢复或重新配对时使用；普通并发调整不需要修改它。
        </div>
      </TechnicalDetails>
    </div>
  );
}
