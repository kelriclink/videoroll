import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { Task, TaskStatus } from "../lib/types";

export default function TasksPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const status = (searchParams.get("status") as TaskStatus | null) ?? null;

  useEffect(() => {
    const qs = new URLSearchParams();
    if (status) qs.set("status", status);
    qs.set("limit", "200");
    fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?${qs.toString()}`)
      .then((data) => setTasks(data))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
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
      "PUBLISHED",
      "FAILED",
    ],
    [],
  );

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-lg font-semibold">Tasks</div>
            <div className="text-sm text-slate-600">任务列表与状态筛选</div>
          </div>
          <Link to="/tasks/new" className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
            新建任务
          </Link>
        </div>

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
                  "rounded border px-2 py-1 text-xs",
                  active ? "border-slate-900 bg-slate-900 text-white" : "bg-white text-slate-700 hover:bg-slate-50",
                ].join(" ")}
              >
                {s ?? "ALL"}
              </button>
            );
          })}
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        {error ? <div className="text-sm text-rose-700">{error}</div> : null}
        {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {tasks ? (
          <div className="overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">ID</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2 pr-3">Created</th>
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
                      <div className="text-xs text-slate-500">{t.source_type}</div>
                      <div className="max-w-[28rem] truncate text-xs text-slate-700">{t.source_url ?? "-"}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="text-xs text-slate-700">{new Date(t.created_at).toLocaleString()}</div>
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

