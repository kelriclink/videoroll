import type { TaskDetailController } from "../useTaskDetailController";
import { Button } from "../../../components/ui";
import StatusBadge from "../../../components/StatusBadge";
import { formatRenderDuration, formatRenderFps, formatRenderSpeed, getRenderTelemetry } from "../../renderTelemetry";

function workflowStepClass(state: "done" | "active" | "pending" | "failed"): string {
  if (state === "done") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (state === "active") return "border-sky-200 bg-sky-50 text-sky-800";
  if (state === "failed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function WorkflowStepper({ steps }: { steps: Array<{ label: string; detail: string; state: "done" | "active" | "pending" | "failed" }> }) {
  return (
    <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
      {steps.map((step, index) => (
        <div key={step.label} className={`rounded-md border p-3 ${workflowStepClass(step.state)}`}>
          <div className="flex items-center gap-2">
            <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-current text-xs font-semibold">
              {index + 1}
            </span>
            <span className="truncate text-sm font-semibold">{step.label}</span>
          </div>
          <div className="mt-2 min-h-8 text-xs leading-4">{step.detail}</div>
        </div>
      ))}
    </div>
  );
}

export function TaskDetailOverview({ controller }: { controller: TaskDetailController }) {
  const {
    setActiveTab,
    task,
    assets,
    busy,
    finalAssets,
    failedSubtitleJobs,
    runningSubtitleJobs,
    failedPublishJobs,
    runningPublishJobs,
    activeRenderExecutions,
    workflowSteps,
    nextAction
  } = controller;
  return (
<div className="space-y-4">
          <div className="vr-section">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">处理流程</div>
                <div className="mt-1 text-xs text-slate-500">按当前任务状态、资产和远程作业推断。</div>
              </div>
              {task ? <StatusBadge status={task.status} /> : null}
            </div>
            <div className="mt-3">{workflowSteps.length ? <WorkflowStepper steps={workflowSteps} /> : <div className="text-sm text-slate-500">等待任务加载。</div>}</div>
            {activeRenderExecutions.length ? <div className="mt-4 space-y-2">
              {activeRenderExecutions.map((execution) => {
                const telemetry = getRenderTelemetry(execution);
                return <div key={execution.id} className="rounded-lg border border-sky-200 bg-sky-50/70 p-3 dark:border-sky-900 dark:bg-sky-950/20">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div><div className="text-sm font-semibold">正在渲染 · {execution.worker_name ?? execution.worker_id.slice(0, 8)}</div><div className="mt-0.5 text-xs text-slate-500">{telemetry.encoder ?? "—"} · {telemetry.deviceName ?? telemetry.backend ?? "—"} · {telemetry.stage}</div></div>
                    <div className="flex flex-wrap gap-4 font-mono text-sm"><span>{formatRenderFps(telemetry.fps)}</span><span className="font-semibold text-sky-700">{formatRenderSpeed(telemetry.speed)}</span><span>ETA {formatRenderDuration(telemetry.etaSeconds)}</span></div>
                  </div>
                  <div className="mt-3 h-2.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800"><div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${telemetry.overallProgress}%` }} /></div>
                  <div className="mt-1 flex flex-wrap justify-between gap-2 text-xs text-slate-500"><span>总进度 {telemetry.overallProgress}% · FFmpeg 渲染 {telemetry.renderPercent.toFixed(1)}%</span><span>已输出 {formatRenderDuration(telemetry.outTimeSeconds)} / {formatRenderDuration(telemetry.durationSeconds)} · 已运行 {formatRenderDuration(telemetry.elapsedSeconds)}</span></div>
                </div>;
              })}
            </div> : null}
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
            <div className="vr-section">
              <div className="text-sm font-semibold">下一步</div>
              {nextAction ? (
                <>
                  <div className="mt-3 text-base font-semibold text-slate-950">{nextAction.title}</div>
                  <div className="mt-1 max-w-3xl text-sm text-slate-600">{nextAction.description}</div>
                  <div className="mt-4 flex flex-wrap items-center gap-2">
                    <Button type="button" tone={nextAction.primaryTone} disabled={busy} onClick={nextAction.onPrimary}>
                      {nextAction.primaryLabel}
                    </Button>
                    {nextAction.secondaryLabel ? (
                      <Button type="button" disabled={busy} onClick={nextAction.onSecondary}>
                        {nextAction.secondaryLabel}
                      </Button>
                    ) : null}
                  </div>
                </>
              ) : (
                <div className="mt-3 text-sm text-slate-500">等待任务加载。</div>
              )}
            </div>

            <div className="vr-section">
              <div className="text-sm font-semibold">当前摘要</div>
              <div className="mt-3 space-y-3 text-sm">
                <button type="button" onClick={() => setActiveTab("media")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">资产</span>
                  <span className="font-medium text-slate-950">{assets?.length ?? "-"}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("subtitle")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">字幕运行 / 失败</span>
                  <span className="font-medium text-slate-950">{runningSubtitleJobs} / {failedSubtitleJobs}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("media")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">最终视频</span>
                  <span className="font-medium text-slate-950">{finalAssets.length}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("publish")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">投稿运行 / 失败</span>
                  <span className={failedPublishJobs ? "font-medium text-rose-700" : "font-medium text-slate-950"}>{runningPublishJobs} / {failedPublishJobs}</span>
                </button>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-slate-500">更新时间</span>
                  <span className="font-medium text-slate-950">{task ? new Date(task.updated_at).toLocaleString() : "-"}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
  );
}
