import { Link } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/http";
import { RealtimeEvent, useRealtimeSubscription } from "../lib/realtime";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { Button, InlineNotice, Metric, PageHeader, TechnicalDetails } from "../components/ui";
import { queueStageLabel, queueStateLabel } from "../lib/executionPresentation";
import { presentTaskError } from "../lib/taskPresentation";

type TaskQueueItem = {
  task_id: string;
  state: string;
  stage: string;
  priority: number;
  subtitle_job_id?: string | null;
  render_job_id?: string | null;
  progress: number;
  stage_elapsed_seconds: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

type TaskQueue = {
  settings: {
    scheduler: "hatchet";
    subtitle_worker_slots: number;
    download_concurrency: number;
    active_downloads: number;
    pending_downloads: number;
  };
  running_count: number;
  queued_count: number;
  tasks: TaskQueueItem[];
};

function elapsedLabel(seconds: number): string {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function priorityLabel(priority: number): string {
  if (priority >= 100) return "紧急";
  if (priority >= 50) return "高";
  if (priority <= -50) return "后台";
  return "普通";
}

function queueErrorTitle(error?: string | null): string {
  return presentTaskError(error)?.title ?? "执行异常";
}

export default function RenderQueuePage() {
  const [queue, setQueue] = useState<TaskQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadConcurrency, setDownloadConcurrency] = useState<number>(2);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const queueRef = useRef<TaskQueue | null>(null);
  const downloadInputDirtyRef = useRef(false);
  const refreshTimerRef = useRef<number | undefined>();

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const q = await fetchJson<TaskQueue>(`${ORCHESTRATOR_URL}/subtitle/task_queue?limit=2000`);
      queueRef.current = q;
      setQueue(q);
      // Realtime queue updates must not overwrite a value while it is edited.
      if (!downloadInputDirtyRef.current) setDownloadConcurrency(q.settings.download_concurrency);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const saveDownloadConcurrency = useCallback(async () => {
    const value = Math.trunc(downloadConcurrency);
    if (!Number.isFinite(value) || value < 1 || value > 16) {
      setSaveMessage("同时下载数应为 1～16");
      return;
    }
    setSaving(true);
    setSaveMessage(null);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/subtitle/task_queue/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_concurrent: value }),
      });
      downloadInputDirtyRef.current = false;
      setSaveMessage("设置已保存，新下载任务自动按此并发数调度");
      await refresh();
    } catch (e: unknown) {
      setSaveMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [downloadConcurrency, refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimerRef.current) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = undefined;
      void refresh();
    }, 250);
  }, [refresh]);

  const handleJobEvent = useCallback((event: RealtimeEvent) => {
    const data = event.data;
    const jobId = String(data.id ?? event.entity_id ?? "");
    const taskId = String(data.task_id ?? "");
    const status = String(data.status ?? "");
    const current = queueRef.current;
    const index = current?.tasks.findIndex((item) => {
      if (item.task_id !== taskId) return false;
      return event.name.startsWith("subtitle_job.") ? item.subtitle_job_id === jobId : item.render_job_id === jobId;
    }) ?? -1;
    if (!current || index < 0 || !["queued", "running"].includes(status)) {
      scheduleRefresh();
      return;
    }

    const item = current.tasks[index];
    const nextState = status === "running" ? "running" : "queued";
    const nextItem: TaskQueueItem = {
      ...item,
      state: nextState,
      stage: status === "running" ? (event.name.startsWith("subtitle_job.") ? "subtitle" : "render") : item.stage,
      progress: Number(data.progress ?? item.progress),
      error_message: typeof data.error_message === "string" ? data.error_message : null,
      updated_at: String(data.updated_at ?? item.updated_at),
    };
    const tasks = [...current.tasks];
    tasks[index] = nextItem;
    const nextQueue = {
      ...current,
      running_count: tasks.filter((row) => row.state === "running").length,
      queued_count: tasks.filter((row) => row.state === "queued").length,
      tasks,
    };
    queueRef.current = nextQueue;
    setQueue(nextQueue);
  }, [scheduleRefresh]);

  const handleRealtimeEvent = useCallback((event: RealtimeEvent) => {
    if (event.name.startsWith("subtitle_job.") || event.name.startsWith("render_job.")) {
      handleJobEvent(event);
      return;
    }
    if (event.name === "task.updated" || event.name === "task.deleted" || event.name === "task_queue.changed") {
      scheduleRefresh();
    }
  }, [handleJobEvent, scheduleRefresh]);

  useRealtimeSubscription(["queue"], handleRealtimeEvent, () => {
    void refresh();
  });

  useEffect(() => () => {
    if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = undefined;
  }, []);

  return (
    <div className="space-y-4">
      <PageHeader
        title="处理队列"
        description="查看正在执行和等待处理的业务阶段。底层调度器、作业 ID 与原始错误放在技术详情中。"
        actions={<Button onClick={() => void refresh()}>刷新</Button>}
      />

      {error ? (
        <InlineNotice title="处理队列未能刷新" tone="danger">
          已保留最后一次成功读取的状态。
          <TechnicalDetails className="mt-2">
            <pre className="whitespace-pre-wrap break-words font-mono">{error}</pre>
          </TechnicalDetails>
        </InlineNotice>
      ) : null}

      <div className="vr-section">
        <div className="grid grid-cols-2 gap-5 lg:grid-cols-4">
          <Metric label="正在处理" value={queue?.running_count ?? 0} tone="info" />
          <Metric label="等待处理" value={queue?.queued_count ?? 0} />
          <Metric label="正在下载" value={(queue?.settings.active_downloads ?? 0) + " / " + (queue?.settings.download_concurrency ?? downloadConcurrency)} />
          <Metric label="等待下载" value={queue?.settings.pending_downloads ?? 0} />
        </div>
        <div className="mt-5 flex flex-wrap items-end gap-3 border-t border-slate-200 pt-4 text-sm dark:border-slate-800">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">同时下载任务上限</span>
            <input className="vr-input w-24" type="number" min={1} max={16} step={1}
              value={downloadConcurrency} onChange={(event) => {
                downloadInputDirtyRef.current = true;
                setDownloadConcurrency(Number(event.target.value));
              }} />
          </label>
          <Button tone="primary"
            disabled={saving || !queue || downloadConcurrency === queue.settings.download_concurrency}
            onClick={() => void saveDownloadConcurrency()}>{saving ? "保存中…" : "保存"}</Button>
          {saveMessage ? <div role="status" className="text-xs text-slate-600">{saveMessage}</div> : null}
        </div>
        <TechnicalDetails className="mt-4" summary="调度器信息">
          <div>调度器：{queue?.settings.scheduler ?? "hatchet"}</div>
          <div className="mt-1">字幕 Worker 槽位：{queue?.settings.subtitle_worker_slots ?? "—"}</div>
          <div className="mt-2 text-slate-500">任务优先级只在创建或恢复工作流时映射到调度器；已提交执行不会伪装成可实时重排。</div>
        </TechnicalDetails>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold text-slate-950 dark:text-white">当前任务</div>
        <div className="mt-1 text-xs text-slate-600">仅显示正在处理或等待处理的业务阶段，并按任务去重。</div>
        {!queue ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {queue && queue.tasks.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
        {queue && queue.tasks.length > 0 ? (
          <>
            <div className="mt-3 grid gap-3 md:hidden">
              {queue.tasks.map((t) => (
                <div key={t.task_id} className="min-w-0 space-y-2 rounded-lg border border-slate-200 p-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-medium">{priorityLabel(t.priority)}</span>
                    <span className="text-xs">{queueStateLabel(t.state)}</span>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span>{queueStageLabel(t.stage)}</span><span className="font-mono">{t.progress}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded bg-slate-100">
                    <div className="h-full rounded bg-sky-600" style={{ width: `${Math.max(0, Math.min(t.progress, 100))}%` }} />
                  </div>
                  <Link to={`/tasks/${t.task_id}`} className="inline-block break-all font-mono text-xs text-sky-700 underline">任务 {t.task_id.slice(0, 8)}</Link>
                  <div className="text-xs text-slate-500">本阶段累计（含排队）：{elapsedLabel(t.stage_elapsed_seconds)}</div>
                  <div className="break-words text-xs text-slate-500">字幕 {t.subtitle_job_id?.slice(0, 8) ?? "-"} · 渲染 {t.render_job_id?.slice(0, 8) ?? "-"}</div>
                  <div className="text-xs text-slate-500">更新 {new Date(t.updated_at).toLocaleString()}</div>
                  {t.error_message ? (
                    <div>
                      <div className="text-xs font-medium text-rose-700 dark:text-rose-300">{queueErrorTitle(t.error_message)}</div>
                      <TechnicalDetails className="mt-1"><div className="break-words font-mono">{t.error_message}</div></TechnicalDetails>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="mt-2 hidden overflow-x-auto md:block">
            <table className="min-w-[920px] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">优先级</th>
                  <th className="py-2 pr-3">状态</th>
                  <th className="py-2 pr-3">阶段</th>
                  <th className="py-2 pr-3">进度</th>
                  <th className="py-2 pr-3">阶段耗时（含等待）</th>
                  <th className="py-2 pr-3">任务</th>
                  <th className="py-2 pr-3">字幕作业</th>
                  <th className="py-2 pr-3">渲染作业</th>
                  <th className="py-2 pr-3">更新时间</th>
                  <th className="py-2 pr-3">异常</th>
                </tr>
              </thead>
              <tbody>
                {queue.tasks.map((t) => (
                  <tr key={t.task_id} className="border-t">
                    <td className="py-2 pr-3 text-xs" title={`VideoRoll priority=${t.priority}`}>{priorityLabel(t.priority)}</td>
                    <td className="py-2 pr-3 text-xs">{queueStateLabel(t.state)}</td>
                    <td className="py-2 pr-3 text-xs">{queueStageLabel(t.stage)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{t.progress}%</td>
                    <td className="py-2 pr-3 text-xs whitespace-nowrap">{elapsedLabel(t.stage_elapsed_seconds)}</td>
                    <td className="py-2 pr-3">
                      <Link className="font-mono text-xs text-slate-900 hover:underline" to={`/tasks/${t.task_id}`}>
                        {t.task_id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{t.subtitle_job_id ? t.subtitle_job_id.slice(0, 8) : "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{t.render_job_id ? t.render_job_id.slice(0, 8) : "-"}</td>
                    <td className="py-2 pr-3 text-xs text-slate-600">{new Date(t.updated_at).toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      {t.error_message ? (
                        <TechnicalDetails summary={queueErrorTitle(t.error_message)}>
                          <div className="max-w-[36rem] whitespace-pre-wrap break-words font-mono">{t.error_message}</div>
                        </TechnicalDetails>
                      ) : <span className="text-xs text-slate-400">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
