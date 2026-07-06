import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { useConfirm, useToast } from "../components/feedbackContext";
import { Button, DataTable, EmptyState, MoreMenu, PageHeader, PaginationControls, Section, TableToolbar, menuItemClass } from "../components/ui";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { Task, TaskStatus } from "../lib/types";

type SubtitleActionResponse = { job_id: string; status: string };
type AutoYouTubeStartResponse = { task_id: string; pipeline_job_id: string };
type RecentFailedResumeResult = { task_id: string; job_id?: string | null; status: string; detail?: string | null };
type RecentFailedResumeResponse = {
  window_hours: number;
  matched_count: number;
  resumed_count: number;
  skipped_count: number;
  failed_count: number;
  results: RecentFailedResumeResult[];
};

const PAGE_SIZE = 25;

const statusOptions: Array<{ value: TaskStatus | null; label: string }> = [
  { value: null, label: "全部" },
  { value: "INGESTED", label: "待处理" },
  { value: "DOWNLOADED", label: "已下载" },
  { value: "ASR_DONE", label: "ASR 完成" },
  { value: "SUBTITLE_READY", label: "字幕完成" },
  { value: "RENDERED", label: "已渲染" },
  { value: "READY_FOR_REVIEW", label: "待审核" },
  { value: "APPROVED", label: "已批准" },
  { value: "PUBLISHED", label: "已发布" },
  { value: "FAILED", label: "失败" },
];

function formatRecentFailedResumeSummary(resp: RecentFailedResumeResponse): string {
  const parts = [`${resp.window_hours} 小时内命中 ${resp.matched_count} 个失败任务`, `已提交 ${resp.resumed_count} 个`];
  if (resp.skipped_count > 0) parts.push(`跳过 ${resp.skipped_count} 个`);
  if (resp.failed_count > 0) parts.push(`失败 ${resp.failed_count} 个`);

  const details = resp.results
    .filter((item) => item.status !== "queued" && !!item.detail?.trim())
    .slice(0, 3)
    .map((item) => `${item.task_id.slice(0, 8)}: ${item.detail}`);

  return details.length ? `${parts.join("，")}。\n${details.join("\n")}` : `${parts.join("，")}。`;
}

function matchesTaskSearch(task: Task, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    task.id,
    task.status,
    task.source_type,
    task.source_url,
    task.display_title,
    task.error_code,
    task.error_message,
    task.created_by,
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(q));
}

export default function TasksPage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionBusyId, setActionBusyId] = useState<string | null>(null);
  const [bulkResumeBusy, setBulkResumeBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [searchText, setSearchText] = useState("");
  const [page, setPage] = useState(0);

  const status = (searchParams.get("status") as TaskStatus | null) ?? null;

  const reloadTasks = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (opts?.silent) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const qs = new URLSearchParams();
        if (status) qs.set("status", status);
        qs.set("limit", "200");
        const data = await fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?${qs.toString()}`);
        setTasks(data);
        return data;
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [status],
  );

  useEffect(() => {
    reloadTasks().catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [reloadTasks]);

  useEffect(() => {
    setPage(0);
  }, [status, searchText]);

  const statusCounts = useMemo(() => {
    const out = new Map<string, number>();
    for (const task of tasks ?? []) out.set(task.status, (out.get(task.status) ?? 0) + 1);
    return out;
  }, [tasks]);

  const filteredTasks = useMemo(() => (tasks ?? []).filter((task) => matchesTaskSearch(task, searchText)), [tasks, searchText]);
  const pageItems = useMemo(() => filteredTasks.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE), [filteredTasks, page]);

  function setStatusFilter(next: TaskStatus | null) {
    const sp = new URLSearchParams(searchParams);
    if (!next) sp.delete("status");
    else sp.set("status", next);
    setSearchParams(sp);
  }

  async function resumeSubtitle(task: Task) {
    const ok = await confirm({
      title: "继续字幕任务",
      message: "会尽量从已有产物处继续（resume=true）。",
      confirmLabel: "继续",
    });
    if (!ok) return;
    setActionBusyId(task.id);
    setError(null);
    try {
      const resp = await fetchJson<SubtitleActionResponse>(`${ORCHESTRATOR_URL}/tasks/${task.id}/actions/subtitle_resume`, {
        method: "POST",
      });
      toast({ kind: "success", title: "已提交继续任务", message: resp.job_id });
      await reloadTasks({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusyId(null);
    }
  }

  async function retryYouTubeDownload(task: Task) {
    const ok = await confirm({
      title: "重试 YouTube 下载",
      message: "会重新拉取视频、元信息和封面。",
      confirmLabel: "重试",
    });
    if (!ok) return;
    setActionBusyId(task.id);
    setError(null);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/tasks/${task.id}/actions/youtube_download`, { method: "POST" });
      await reloadTasks({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusyId(null);
    }
  }

  async function startAuto(task: Task) {
    const ok = await confirm({
      title: "启动 YouTube 自动模式",
      message: "会继续执行下载、字幕、压制和自动投稿。",
      confirmLabel: "启动",
      tone: "warning",
    });
    if (!ok) return;
    setActionBusyId(task.id);
    setError(null);
    try {
      const resp = await fetchJson<AutoYouTubeStartResponse>(`${ORCHESTRATOR_URL}/tasks/${task.id}/actions/auto_youtube_start`, {
        method: "POST",
      });
      toast({ kind: "success", title: "已提交自动任务", message: resp.pipeline_job_id });
      await reloadTasks({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusyId(null);
    }
  }

  async function resumeRecentFailed() {
    const ok = await confirm({
      title: "继续最近失败任务",
      message: "一键继续最近 24 小时内失败任务？会批量恢复字幕任务，并在渲染后自动投稿。",
      confirmLabel: "继续",
      tone: "warning",
    });
    if (!ok) return;
    setBulkResumeBusy(true);
    setError(null);
    try {
      const resp = await fetchJson<RecentFailedResumeResponse>(`${ORCHESTRATOR_URL}/tasks/actions/resume_failed_recent`, {
        method: "POST",
      });
      toast({ kind: "info", title: "批量恢复已处理", message: formatRecentFailedResumeSummary(resp) });
      await reloadTasks({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBulkResumeBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="任务"
        description="查看处理状态、恢复失败任务，或进入任务详情继续操作。"
        actions={
          <>
            <Button disabled={bulkResumeBusy} tone="warning" onClick={resumeRecentFailed}>
              {bulkResumeBusy ? "处理中..." : "继续 24h 失败任务"}
            </Button>
            <Link to="/tasks/new" className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
              新建任务
            </Link>
          </>
        }
      />

      <Section>
        <TableToolbar
          title="任务列表"
          description="最多加载最近 200 条；搜索和分页在当前结果内完成。"
          meta={
            tasks
              ? `已加载 ${tasks.length} 条，当前显示 ${filteredTasks.length} 条${refreshing ? "，刷新中..." : ""}`
              : loading
                ? "加载中..."
                : undefined
          }
          actions={
            <Button disabled={refreshing || loading} onClick={() => reloadTasks({ silent: true }).catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))}>
              {refreshing ? "刷新中..." : "刷新"}
            </Button>
          }
          filters={
            <>
              <input
                className="vr-input w-full lg:w-80"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="搜索标题、链接、ID、错误信息"
              />
              <div className="flex flex-wrap items-center gap-2">
                {statusOptions.map((option) => {
                  const active = (option.value ?? "") === (status ?? "");
                  const count = option.value ? statusCounts.get(option.value) : tasks?.length;
                  return (
                    <button
                      key={option.value ?? "all"}
                      type="button"
                      onClick={() => setStatusFilter(option.value)}
                      className={[
                        "rounded-md border px-2 py-1 text-xs",
                        active ? "border-slate-900 bg-slate-900 text-white" : "bg-white text-slate-700 hover:bg-slate-50",
                      ].join(" ")}
                    >
                      {option.label}
                      {typeof count === "number" ? <span className="ml-1 opacity-70">{count}</span> : null}
                    </button>
                  );
                })}
              </div>
            </>
          }
        />

        {error ? <div className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
        {loading && !tasks ? <div className="mt-3 text-sm text-slate-500">加载中...</div> : null}
        {tasks ? (
          <>
            {filteredTasks.length === 0 ? (
              <EmptyState>没有匹配的任务</EmptyState>
            ) : (
              <DataTable wrapClassName="mt-3">
                <thead>
                  <tr>
                    <th className="w-24">ID</th>
                    <th className="w-40">状态</th>
                    <th>来源</th>
                    <th className="w-44">创建时间</th>
                    <th className="w-44 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {pageItems.map((task) => {
                    const isBusy = actionBusyId === task.id;
                    const canRetryDownload = task.status === "FAILED" && task.source_type === "youtube" && !!(task.source_url ?? "").trim();
                    const canStartAuto = ["INGESTED", "DOWNLOADED"].includes(task.status) && task.source_type === "youtube" && !!(task.source_url ?? "").trim();
                    const primaryAction =
                      task.status === "FAILED"
                        ? "resume"
                        : canStartAuto
                          ? "auto"
                          : "detail";
                    const hasMoreActions = primaryAction !== "detail" || canRetryDownload;
                    return (
                      <tr key={task.id}>
                        <td>
                          <Link to={`/tasks/${task.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                            {task.id.slice(0, 8)}
                          </Link>
                        </td>
                        <td>
                          <StatusBadge status={task.status} />
                        </td>
                        <td>
                          {task.display_title?.trim() ? (
                            <div className="max-w-[34rem] truncate text-sm font-semibold text-slate-900">{task.display_title}</div>
                          ) : null}
                          <div className="text-xs text-slate-500">{task.source_type}</div>
                          <div className="max-w-[34rem] truncate text-xs text-slate-700">{task.source_url ?? "-"}</div>
                          {task.error_message ? (
                            <details className="mt-1 max-w-[34rem] text-xs text-rose-700">
                              <summary className="cursor-pointer truncate" title={task.error_message}>
                                {task.error_message}
                              </summary>
                              <div className="mt-1 whitespace-pre-wrap break-words rounded-md bg-rose-50 p-2">{task.error_message}</div>
                            </details>
                          ) : null}
                        </td>
                        <td>
                          <div className="text-xs text-slate-700">{new Date(task.created_at).toLocaleString()}</div>
                        </td>
                        <td>
                          <div className="flex items-center justify-end gap-2">
                            {primaryAction === "resume" ? (
                              <Button size="xs" disabled={isBusy} onClick={() => resumeSubtitle(task)}>
                                继续字幕
                              </Button>
                            ) : primaryAction === "auto" ? (
                              <Button size="xs" disabled={isBusy} onClick={() => startAuto(task)}>
                                启动自动
                              </Button>
                            ) : (
                              <Link className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-800 hover:bg-slate-50" to={`/tasks/${task.id}`}>
                                详情
                              </Link>
                            )}
                            {hasMoreActions ? (
                              <MoreMenu>
                                {primaryAction !== "detail" ? (
                                  <Link className={menuItemClass} to={`/tasks/${task.id}`}>
                                    查看详情
                                  </Link>
                                ) : null}
                                {canRetryDownload ? (
                                  <button className={menuItemClass} disabled={isBusy} onClick={() => retryYouTubeDownload(task)}>
                                    重试下载
                                  </button>
                                ) : null}
                              </MoreMenu>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </DataTable>
            )}
            <PaginationControls
              page={page}
              pageSize={PAGE_SIZE}
              totalItems={filteredTasks.length}
              currentCount={pageItems.length}
              disabled={loading}
              onPrev={() => setPage((value) => Math.max(0, value - 1))}
              onNext={() => setPage((value) => value + 1)}
            />
          </>
        ) : null}
      </Section>
    </div>
  );
}
