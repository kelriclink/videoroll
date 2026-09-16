import type { TaskDetailController } from "../useTaskDetailController";
import StatusBadge from "../../../components/StatusBadge";
import { Button } from "../../../components/ui";
import type { TaskDetailTab } from "../types";

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
            <div className="text-lg font-semibold">任务详情</div>
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

        {error ? <div className="mt-3 whitespace-pre-wrap break-words text-sm text-rose-700">{error}</div> : null}
        {coreError ? <div className="mt-3 whitespace-pre-wrap break-words text-sm text-rose-700">任务核心数据加载失败：{coreError}</div> : null}
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
              <div className="mt-1 text-sm text-slate-800">
                {task.source_type} · {task.source_license}
              </div>
              <div className="mt-1 break-all text-xs text-slate-600">{task.source_url ?? "-"}</div>
            </div>
          </div>
        ) : null}
        {task?.error_message ? (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <div className="text-xs text-amber-700">Task Message</div>
            <div className="mt-1 whitespace-pre-wrap break-words">{task.error_message}</div>
          </div>
        ) : null}

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
