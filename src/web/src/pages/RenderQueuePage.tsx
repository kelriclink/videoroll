import { Link } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/http";
import { RealtimeEvent, useRealtimeSubscription } from "../lib/realtime";
import { ORCHESTRATOR_URL } from "../lib/urls";

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
  if (priority >= 50) return priority >= 100 ? "P0 紧急 · HIGH" : "P1 高 · HIGH";
  if (priority <= -50) return "P3 后台 · LOW";
  return "P2 普通 · MEDIUM";
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
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Workflow Queue</div>
        <div className="mt-1 text-sm text-slate-600">
          调度器已切换为 Hatchet。这里直接展示 SubtitleJob / RenderJob / YouTube 下载的真实执行状态，不再使用旧 Task lock 推断运行状态。
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-semibold">Runtime</div>
          <button onClick={() => void refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        <div className="mt-3 grid gap-3 text-sm sm:grid-cols-4">
          <div><div className="text-xs text-slate-500">Scheduler</div><div className="mt-1 font-mono">{queue?.settings.scheduler ?? "hatchet"}</div></div>
          <div><div className="text-xs text-slate-500">Subtitle slots</div><div className="mt-1 font-mono">{queue?.settings.subtitle_worker_slots ?? "-"}</div></div>
          <div><div className="text-xs text-slate-500">Running</div><div className="mt-1 font-mono">{queue?.running_count ?? 0}</div></div>
          <div><div className="text-xs text-slate-500">Queued</div><div className="mt-1 font-mono">{queue?.queued_count ?? 0}</div></div>
        </div>
        <div className="mt-4 flex flex-wrap items-end gap-3 border-t pt-4 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">同时下载任务数（持久化）</span>
            <input className="w-24 rounded border px-2 py-2 font-mono" type="number" min={1} max={16} step={1}
              value={downloadConcurrency} onChange={(event) => {
                downloadInputDirtyRef.current = true;
                setDownloadConcurrency(Number(event.target.value));
              }} />
          </label>
          <button className="rounded border bg-slate-900 px-3 py-2 text-white disabled:opacity-50"
            disabled={saving || !queue || downloadConcurrency === queue.settings.download_concurrency}
            onClick={() => void saveDownloadConcurrency()}>{saving ? "保存中…" : "保存并发"}</button>
          <div className="text-xs text-slate-600">正在下载：{queue?.settings.active_downloads ?? 0} / {queue?.settings.download_concurrency ?? 2} · 等待下载：{queue?.settings.pending_downloads ?? 0}</div>
          {saveMessage ? <div role="status" className="text-xs text-slate-600">{saveMessage}</div> : null}
        </div>
        <div className="mt-3 text-xs text-slate-500">
          Subtitle slots 来自 HATCHET_SUBTITLE_WORKER_SLOTS。Task priority 会在创建或恢复 Hatchet run 时映射为 HIGH / MEDIUM / LOW；已提交 run 不做伪实时重排。
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Active Runs</div>
        <div className="mt-1 text-xs text-slate-600">仅显示当前 running / queued 的业务阶段，按 Task 去重。</div>
        {!queue ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {queue && queue.tasks.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
        {queue && queue.tasks.length > 0 ? (
          <>
            <div className="mt-3 grid gap-3 md:hidden">
              {queue.tasks.map((t) => (
                <div key={t.task_id} className="min-w-0 space-y-2 rounded-lg border border-slate-200 p-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-medium">{priorityLabel(t.priority)}</span>
                    <span className="font-mono text-xs">{t.state}</span>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span>阶段：{t.stage}</span><span className="font-mono">{t.progress}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded bg-slate-100">
                    <div className="h-full rounded bg-sky-600" style={{ width: `${Math.max(0, Math.min(t.progress, 100))}%` }} />
                  </div>
                  <Link to={`/tasks/${t.task_id}`} className="inline-block break-all font-mono text-xs text-sky-700 underline">任务 {t.task_id.slice(0, 8)}</Link>
                  <div className="text-xs text-slate-500">本阶段累计（含排队）：{elapsedLabel(t.stage_elapsed_seconds)}</div>
                  <div className="break-words text-xs text-slate-500">字幕 {t.subtitle_job_id?.slice(0, 8) ?? "-"} · 渲染 {t.render_job_id?.slice(0, 8) ?? "-"}</div>
                  <div className="text-xs text-slate-500">更新 {new Date(t.updated_at).toLocaleString()}</div>
                  {t.error_message ? <div className="break-words text-xs text-rose-700">{t.error_message}</div> : null}
                </div>
              ))}
            </div>
            <div className="mt-2 hidden overflow-x-auto md:block">
            <table className="min-w-[920px] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Priority</th>
                  <th className="py-2 pr-3">State</th>
                  <th className="py-2 pr-3">Stage</th>
                  <th className="py-2 pr-3">Progress</th>
                  <th className="py-2 pr-3">阶段耗时（含等待）</th>
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">Subtitle Job</th>
                  <th className="py-2 pr-3">Render Job</th>
                  <th className="py-2 pr-3">Updated</th>
                  <th className="py-2 pr-3">Error</th>
                </tr>
              </thead>
              <tbody>
                {queue.tasks.map((t) => (
                  <tr key={t.task_id} className="border-t">
                    <td className="py-2 pr-3 text-xs" title={`VideoRoll priority=${t.priority}`}>{priorityLabel(t.priority)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{t.state}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{t.stage}</td>
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
                        <div className="max-w-[36rem] truncate text-xs text-rose-700" title={t.error_message}>{t.error_message}</div>
                      ) : <span className="text-xs text-slate-400">-</span>}
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
