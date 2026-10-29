import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { InlineNotice, Metric, PageHeader, Section, TechnicalDetails } from "../components/ui";
import { fetchJson } from "../lib/http";
import { RealtimeEvent, useRealtimeSubscription } from "../lib/realtime";
import { presentTaskError, taskIsInProgress, taskNeedsAttention } from "../lib/taskPresentation";
import type { Asset, Task } from "../lib/types";
import { ORCHESTRATOR_URL } from "../lib/urls";

type ConvertedVideoItem = {
  task: Task;
  final_asset: Asset;
  cover_asset?: Asset | null;
};

type AlertItem = {
  id: string;
  severity: string;
  title: string;
  message: string;
  details?: Record<string, unknown>;
  last_seen_at: string;
};

type ResourceSnapshot = {
  sampled_at: string;
  cpu: { percent?: number | null; cores: number; load_average?: number[] | null };
  memory: {
    total_bytes: number;
    used_bytes: number;
    available_bytes: number;
    percent?: number | null;
  };
};

function taskTitle(task: Task) {
  const title = task.display_title?.trim();
  if (title) return title;
  if (task.source_url?.trim()) return task.source_url;
  return `任务 ${task.id.slice(0, 8)}`;
}

function fileNameFromKey(key: string): string {
  const parts = String(key || "").split("/");
  return parts[parts.length - 1] || key || "视频成品";
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

function formatBytes(value?: number | null): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, Number(value || 0));
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function upsertTask(current: Task[] | null, task: Task, limit = 200): Task[] {
  const rows = current ?? [];
  const index = rows.findIndex((item) => item.id === task.id);
  if (index < 0) return [task, ...rows].slice(0, limit);
  const next = [...rows];
  next[index] = { ...rows[index], ...task };
  return next;
}

function updateTaskUpload(current: Task[] | null, job: Record<string, unknown>): Task[] | null {
  if (!current) return current;
  const taskId = String(job.task_id ?? "");
  const jobId = String(job.id ?? "");
  return current.map((task) => {
    if (task.id !== taskId) return task;
    if (job.upload_active) {
      return {
        ...task,
        bilibili_upload: {
          job_id: jobId,
          progress: Math.max(0, Math.min(100, Math.floor(Number(job.upload_progress ?? 0)))),
        },
      };
    }
    return task.bilibili_upload?.job_id === jobId ? { ...task, bilibili_upload: null } : task;
  });
}

function TaskRow({ task }: { task: Task }) {
  return (
    <Link
      to={`/tasks/${task.id}`}
      className="block border-t border-slate-100 py-3 first:border-0 hover:bg-slate-50/70 dark:border-slate-800 dark:hover:bg-slate-900/60"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-slate-950 dark:text-white">{taskTitle(task)}</div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {task.source_type === "youtube" ? "YouTube" : task.source_type} · {formatDate(task.updated_at)}
          </div>
        </div>
        <StatusBadge status={task.status} />
      </div>
      {task.bilibili_upload ? (
        <div className="mt-2">
          <div className="flex justify-between text-[11px] text-slate-500 dark:text-slate-400">
            <span>正在上传到 B 站</span>
            <span>{task.bilibili_upload.progress}%</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
            <div className="h-full rounded-full bg-sky-500" style={{ width: `${task.bilibili_upload.progress}%` }} />
          </div>
        </div>
      ) : null}
    </Link>
  );
}

function FailedTaskRow({ task }: { task: Task }) {
  const error = presentTaskError(task.error_message, task.error_code);
  return (
    <div className="border-t border-slate-100 py-4 first:border-0 dark:border-slate-800">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Link to={`/tasks/${task.id}`} className="font-medium text-slate-950 hover:underline dark:text-white">
            {taskTitle(task)}
          </Link>
          <div className="mt-1 text-sm font-medium text-rose-700 dark:text-rose-300">{error?.title ?? "任务需要处理"}</div>
          {error?.detail ? <div className="mt-1 text-sm text-slate-600 dark:text-slate-300">{error.detail}</div> : null}
          {error?.actionHint ? <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{error.actionHint}</div> : null}
        </div>
        <Link
          to={`/tasks/${task.id}`}
          className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
        >
          处理
        </Link>
      </div>
      {task.error_message ? (
        <TechnicalDetails className="mt-3">
          <pre className="whitespace-pre-wrap break-words font-mono">{task.error_message}</pre>
        </TechnicalDetails>
      ) : null}
    </div>
  );
}

export default function DashboardPage() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [videos, setVideos] = useState<ConvertedVideoItem[] | null>(null);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [resources, setResources] = useState<ResourceSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const failures: string[] = [];
    const [taskResult, videoResult, alertResult, resourceResult] = await Promise.allSettled([
      fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?limit=200`),
      fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=8`),
      fetchJson<AlertItem[]>(`${ORCHESTRATOR_URL}/operations/alerts?status=active&limit=8`),
      fetchJson<ResourceSnapshot>(`${ORCHESTRATOR_URL}/system/resources`),
    ]);

    if (taskResult.status === "fulfilled") setTasks(taskResult.value);
    else failures.push(taskResult.reason instanceof Error ? taskResult.reason.message : String(taskResult.reason));

    if (videoResult.status === "fulfilled") setVideos(videoResult.value);
    else failures.push(videoResult.reason instanceof Error ? videoResult.reason.message : String(videoResult.reason));

    if (alertResult.status === "fulfilled") setAlerts(alertResult.value);
    if (resourceResult.status === "fulfilled") setResources(resourceResult.value);

    setLoadError(failures.length ? failures.join("\n") : null);
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const handleRealtimeEvent = useCallback((event: RealtimeEvent) => {
    const data = event.data;
    const id = String(data.id ?? event.entity_id ?? "");
    if (event.name === "task.updated") {
      setTasks((current) => upsertTask(current, data as unknown as Task));
      return;
    }
    if (event.name === "task.deleted") {
      setTasks((current) => current?.filter((task) => task.id !== id) ?? current);
      return;
    }
    if (event.name === "publish_job.updated" && String(data.platform ?? "") === "bilibili") {
      setTasks((current) => updateTaskUpload(current, data));
      return;
    }
    if (event.name === "publish_job.deleted") {
      setTasks((current) => updateTaskUpload(current, { ...data, id, upload_active: false }));
      return;
    }
    if (event.name === "system.resources.sample") {
      setResources(data as unknown as ResourceSnapshot);
    }
  }, []);

  useRealtimeSubscription(["tasks", "publishing", "resources"], handleRealtimeEvent, () => void load());

  const failedTasks = useMemo(() => (tasks ?? []).filter(taskNeedsAttention).slice(0, 6), [tasks]);
  const activeTasks = useMemo(
    () => (tasks ?? []).filter((task) => taskIsInProgress(task) && !taskNeedsAttention(task)).slice(0, 8),
    [tasks],
  );
  const publishedCount = useMemo(() => (tasks ?? []).filter((task) => task.status === "PUBLISHED").length, [tasks]);
  const stoppedCount = useMemo(() => (tasks ?? []).filter((task) => task.status === "CANCELED").length, [tasks]);
  const memoryPercent = Number(resources?.memory.percent ?? 0);
  const resourceWarning = memoryPercent >= 90
    ? `内存占用较高：${memoryPercent.toFixed(1)}%（${formatBytes(resources?.memory.used_bytes)} / ${formatBytes(resources?.memory.total_bytes)}）`
    : null;
  const hasSystemIssue = alerts.length > 0 || Boolean(resourceWarning);

  return (
    <div className="space-y-5">
      <PageHeader
        title="工作台"
        description="先处理异常，再看正在制作和最近完成。技术诊断集中在运维页面。"
        actions={
          <>
            <Link
              to="/tasks"
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
            >
              全部任务
            </Link>
            <Link to="/tasks/new" className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-950">
              新建任务
            </Link>
          </>
        }
      />

      {loadError ? (
        <InlineNotice title="工作台部分数据未能刷新" tone="warning">
          已保留成功加载的数据。
          <TechnicalDetails className="mt-2">
            <pre className="whitespace-pre-wrap break-words font-mono">{loadError}</pre>
          </TechnicalDetails>
        </InlineNotice>
      ) : null}

      <Section>
        <div className="grid grid-cols-2 gap-5 lg:grid-cols-4">
          <Metric label="正在制作" value={activeTasks.length} detail="当前列表中仍在推进的任务" tone="info" />
          <Metric label="需要处理" value={failedTasks.length} detail="失败或需要人工介入" tone={failedTasks.length ? "danger" : "success"} />
          <Metric label="已停止" value={stoppedCount} detail="可在任务页恢复" />
          <Metric label="已发布" value={publishedCount} detail="最近加载的 200 条任务" tone="success" />
        </div>
      </Section>

      {failedTasks.length > 0 ? (
        <Section>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950 dark:text-white">需要处理</div>
              <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">优先显示会阻断生产流程的失败任务。</div>
            </div>
            <Link to="/tasks?status=FAILED" className="text-sm text-sky-700 hover:underline dark:text-sky-300">查看全部</Link>
          </div>
          <div className="mt-2">{failedTasks.map((task) => <FailedTaskRow key={task.id} task={task} />)}</div>
        </Section>
      ) : null}

      <Section>
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-950 dark:text-white">正在制作</div>
            <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">这里只展示业务阶段；执行器、批次和 Agent 细节放到工作流诊断。</div>
          </div>
          <Link to="/queue/render" className="text-sm text-sky-700 hover:underline dark:text-sky-300">查看处理队列</Link>
        </div>
        <div className="mt-2">
          {!tasks ? <div className="py-6 text-sm text-slate-500">加载中…</div> : null}
          {tasks && activeTasks.length === 0 ? <div className="py-6 text-sm text-slate-500">当前没有正在制作的任务。</div> : null}
          {activeTasks.map((task) => <TaskRow key={task.id} task={task} />)}
        </div>
      </Section>

      <Section>
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-950 dark:text-white">最近视频成品</div>
            <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">可进入任务详情继续发布、下载或加入播控。</div>
          </div>
          <Link to="/videos" className="text-sm text-sky-700 hover:underline dark:text-sky-300">管理全部</Link>
        </div>
        <div className="mt-2 divide-y divide-slate-100 dark:divide-slate-800">
          {!videos ? <div className="py-6 text-sm text-slate-500">加载中…</div> : null}
          {videos?.length === 0 ? <div className="py-6 text-sm text-slate-500">暂无视频成品。</div> : null}
          {videos?.map((item) => (
            <div key={item.final_asset.id} className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <Link to={`/tasks/${item.task.id}`} className="block truncate text-sm font-medium text-slate-950 hover:underline dark:text-white">
                  {item.task.display_title?.trim() || fileNameFromKey(item.final_asset.storage_key)}
                </Link>
                <div className="mt-1 flex items-center gap-2 text-xs text-slate-500">
                  <StatusBadge status={item.task.status} compact />
                  <span>{formatDate(item.final_asset.created_at)}</span>
                </div>
              </div>
              <div className="flex shrink-0 gap-2">
                <a
                  href={`${ORCHESTRATOR_URL}/tasks/${item.task.id}/assets/${item.final_asset.id}/download`}
                  className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
                >
                  下载
                </a>
                <Link to={`/tasks/${item.task.id}`} className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900">
                  详情
                </Link>
              </div>
            </div>
          ))}
        </div>
      </Section>

      {hasSystemIssue ? (
        <Section>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950 dark:text-white">系统提醒</div>
              <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">只在存在需要关注的系统状态时显示。</div>
            </div>
            <Link to="/operations" className="text-sm text-sky-700 hover:underline dark:text-sky-300">进入运维</Link>
          </div>
          <div className="mt-3 space-y-2">
            {resourceWarning ? <InlineNotice tone="warning">{resourceWarning}</InlineNotice> : null}
            {alerts.slice(0, 4).map((alert) => (
              <InlineNotice key={alert.id} title={alert.title} tone={alert.severity === "critical" ? "danger" : "warning"}>
                {alert.message || "检测到需要处理的系统告警。"}
              </InlineNotice>
            ))}
          </div>
        </Section>
      ) : (
        <div className="px-1 text-xs text-slate-400">系统未报告需要处理的告警。</div>
      )}
    </div>
  );
}
