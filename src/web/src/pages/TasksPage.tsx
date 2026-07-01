import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { useConfirm, useToast } from "../components/feedbackContext";
import { Button, PageHeader } from "../components/ui";
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

export default function TasksPage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionBusyId, setActionBusyId] = useState<string | null>(null);
  const [bulkResumeBusy, setBulkResumeBusy] = useState(false);

  const status = (searchParams.get("status") as TaskStatus | null) ?? null;

  async function reloadTasks() {
    const qs = new URLSearchParams();
    if (status) qs.set("status", status);
    qs.set("limit", "200");
    const data = await fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?${qs.toString()}`);
    setTasks(data);
    return data;
  }

  useEffect(() => {
    setError(null);
    reloadTasks()
      .then(() => undefined)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const statusOptions = useMemo(
    () => [
      null,
      "CREATED",
      "INGESTED",
      "DOWNLOADED",
      "AUDIO_EXTRACTED",
      "ASR_DONE",
      "SUBTITLE_READY",
      "RENDERED",
      "READY_FOR_REVIEW",
      "APPROVED",
      "PUBLISHED",
      "FAILED",
    ],
    [],
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Tasks"
        description="任务列表与状态筛选"
        actions={
          <>
            <Button
              disabled={bulkResumeBusy}
              onClick={async () => {
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
                  await reloadTasks();
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBulkResumeBusy(false);
                }
              }}
            >
              一键继续24h失败任务
            </Button>
            <Link to="/tasks/new" className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
              新建任务
            </Link>
          </>
        }
      />

      <div className="vr-section">
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="text-xs text-slate-600">状态：</div>
          {statusOptions.map((s) => {
            const active = (s ?? "") === (status ?? "");
            return (
              <button
                key={s ?? "all"}
                onClick={() => {
                  const sp = new URLSearchParams(searchParams);
                  if (!s) sp.delete("status");
                  else sp.set("status", s);
                  setSearchParams(sp);
                }}
                className={[
                  "rounded-md border px-2 py-1 text-xs",
                  active ? "border-slate-900 bg-slate-900 text-white" : "bg-white text-slate-700 hover:bg-slate-50",
                ].join(" ")}
              >
                {s ?? "ALL"}
              </button>
            );
          })}
        </div>
      </div>

      <div className="vr-section">
        {error ? <div className="text-sm text-rose-700">{error}</div> : null}
        {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {tasks ? (
          <div className="overflow-auto rounded-md border border-slate-200">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">ID</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2 pr-3">Created</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.id} className="border-t">
                    <td className="py-2 pr-3">
                      <Link to={`/tasks/${t.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                        {t.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="py-2 pr-3">
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="py-2 pr-3">
                      {t.display_title?.trim() ? (
                        <div className="max-w-[28rem] truncate text-sm font-semibold text-slate-900">{t.display_title}</div>
                      ) : null}
                      <div className="text-xs text-slate-500">{t.source_type}</div>
                      <div className="max-w-[28rem] truncate text-xs text-slate-700">{t.source_url ?? "-"}</div>
                      {t.error_message ? (
                        <details className="mt-1 max-w-[28rem] text-xs text-rose-700">
                          <summary className="cursor-pointer truncate" title={t.error_message}>
                          {t.error_message}
                          </summary>
                          <div className="mt-1 whitespace-pre-wrap break-words rounded-md bg-rose-50 p-2">{t.error_message}</div>
                        </details>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3">
                      <div className="text-xs text-slate-700">{new Date(t.created_at).toLocaleString()}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        {t.status === "FAILED" ? (
                          <button
                            disabled={actionBusyId === t.id}
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                            onClick={async () => {
                              const ok = await confirm({
                                title: "继续字幕任务",
                                message: "会尽量从已有产物处继续（resume=true）。",
                                confirmLabel: "继续",
                              });
                              if (!ok) return;
                              setActionBusyId(t.id);
                              setError(null);
                              try {
                                const resp = await fetchJson<SubtitleActionResponse>(`${ORCHESTRATOR_URL}/tasks/${t.id}/actions/subtitle_resume`, {
                                  method: "POST",
                                });
                                toast({ kind: "success", title: "已提交继续任务", message: resp.job_id });
                                await reloadTasks();
                              } catch (e: unknown) {
                                setError(e instanceof Error ? e.message : String(e));
                              } finally {
                                setActionBusyId(null);
                              }
                            }}
                          >
                            继续字幕
                          </button>
                        ) : null}
                        {t.status === "FAILED" && t.source_type === "youtube" && !!(t.source_url ?? "").trim() ? (
                          <button
                            disabled={actionBusyId === t.id}
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                            onClick={async () => {
                              const ok = await confirm({
                                title: "重试 YouTube 下载",
                                message: "会重新拉取视频、元信息和封面。",
                                confirmLabel: "重试",
                              });
                              if (!ok) return;
                              setActionBusyId(t.id);
                              setError(null);
                              try {
                                await fetchJson(`${ORCHESTRATOR_URL}/tasks/${t.id}/actions/youtube_download`, { method: "POST" });
                                await reloadTasks();
                              } catch (e: unknown) {
                                setError(e instanceof Error ? e.message : String(e));
                              } finally {
                                setActionBusyId(null);
                              }
                            }}
                          >
                            重试下载
                          </button>
                        ) : null}
                        {["INGESTED", "DOWNLOADED"].includes(t.status) && t.source_type === "youtube" && !!(t.source_url ?? "").trim() ? (
                          <button
                            disabled={actionBusyId === t.id}
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                            onClick={async () => {
                              const ok = await confirm({
                                title: "启动 YouTube 自动模式",
                                message: "会继续执行下载、字幕、压制和自动投稿。",
                                confirmLabel: "启动",
                                tone: "warning",
                              });
                              if (!ok) return;
                              setActionBusyId(t.id);
                              setError(null);
                              try {
                                const resp = await fetchJson<AutoYouTubeStartResponse>(
                                  `${ORCHESTRATOR_URL}/tasks/${t.id}/actions/auto_youtube_start`,
                                  { method: "POST" },
                                );
                                toast({ kind: "success", title: "已提交自动任务", message: resp.pipeline_job_id });
                                await reloadTasks();
                              } catch (e: unknown) {
                                setError(e instanceof Error ? e.message : String(e));
                              } finally {
                                setActionBusyId(null);
                              }
                            }}
                          >
                            启动自动
                          </button>
                        ) : null}
                        <Link className="rounded border px-2 py-1 text-xs hover:bg-slate-50" to={`/tasks/${t.id}`}>
                          Detail
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  );
}
