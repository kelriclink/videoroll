import type { TaskDetailController } from "../useTaskDetailController";
import StatusBadge from "../../../components/StatusBadge";
import { Button, InlineNotice, TechnicalDetails } from "../../../components/ui";
import type { TaskDetailTab } from "../types";
import { presentTaskError } from "../../../lib/taskPresentation";

export function TaskDetailHeader({ controller }: { controller: TaskDetailController }) {
  const {
    taskId,
    activeTab,
    setActiveTab,
    task,
    assets,
    runningSubtitleJobs,
    failedSubtitleJobs,
    publishJobs,
    logAssets,
    error,
    coreError,
    busy,
    refresh,
    stopTask,
    resumeStoppedTask
  } = controller;
  if (!taskId) return null;

  const tabs: Array<{ id: TaskDetailTab; label: string; badge?: string | number | null }> = [
    { id: "overview", label: "概览" },
    { id: "media", label: "媒体与资产", badge: assets?.length ?? null },
    { id: "subtitle", label: "字幕 / 渲染", badge: runningSubtitleJobs || failedSubtitleJobs || null },
    { id: "publish", label: "投稿", badge: publishJobs?.length ?? null },
    { id: "logs", label: "日志", badge: logAssets.length || null },
  ];

  return (
      <div className="vr-section">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-semibold text-slate-950 dark:text-white">任务详情</div>
            <div className="mt-1 font-mono text-xs text-slate-600">{taskId}</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {task?.status === "CANCELED" ? (
              <Button disabled={busy} onClick={resumeStoppedTask}>
                恢复任务
              </Button>
            ) : task && ["CREATED", "INGESTED", "DOWNLOADED", "AUDIO_EXTRACTED", "ASR_DONE", "TRANSLATED", "SUBTITLE_READY", "RENDERED", "READY_FOR_REVIEW", "APPROVED"].includes(task.status) ? (
              <Button disabled={busy} tone="danger" onClick={stopTask}>
                停止任务
              </Button>
            ) : null}
            <Button disabled={busy} onClick={() => refresh()}>
              刷新
            </Button>
          </div>
        </div>

        {error ? (
          <InlineNotice className="mt-3" title="任务详情部分操作失败" tone="danger">
            <TechnicalDetails className="mt-2"><pre className="whitespace-pre-wrap break-words font-mono">{error}</pre></TechnicalDetails>
          </InlineNotice>
        ) : null}
        {coreError ? (
          <InlineNotice className="mt-3" title="任务核心数据加载失败" tone="danger">
            <TechnicalDetails className="mt-2"><pre className="whitespace-pre-wrap break-words font-mono">{coreError}</pre></TechnicalDetails>
          </InlineNotice>
        ) : null}
        {!task ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
        {task ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">状态</div>
              <div className="mt-1">
                <StatusBadge status={task.status} />
              </div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">来源</div>
              <div className="mt-1 text-sm text-slate-800 dark:text-slate-200">
                {task.source_type} · {task.source_license}
              </div>
              <div className="mt-1 break-all text-xs text-slate-600">{task.source_url ?? "-"}</div>
            </div>
          </div>
        ) : null}
        {task?.error_message ? (() => {
          const failure = presentTaskError(task.error_message, task.error_code);
          return (
            <InlineNotice className="mt-3" title={failure?.title ?? "任务需要处理"} tone="warning">
              {failure?.detail ?? "任务报告了异常。"}
              {failure?.actionHint ? <div className="mt-1 text-xs">{failure.actionHint}</div> : null}
              <TechnicalDetails className="mt-2">
                <pre className="whitespace-pre-wrap break-words font-mono">{task.error_message}</pre>
              </TechnicalDetails>
            </InlineNotice>
          );
        })() : null}

        <div className="mt-4 flex gap-1 overflow-x-auto border-b border-slate-200">
          {tabs.map((tab) => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={[
                  "mb-[-1px] inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-2 text-sm",
                  active
                    ? "border-slate-900 text-slate-950"
                    : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-950",
                ].join(" ")}
              >
                {tab.label}
                {tab.badge ? <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">{tab.badge}</span> : null}
              </button>
            );
          })}
        </div>
      </div>
  );
}
