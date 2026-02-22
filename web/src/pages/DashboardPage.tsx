import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import StatusBadge from "../components/StatusBadge";
import { Asset, Task } from "../lib/types";

type ConvertedVideoItem = {
  task: Task;
  final_asset: Asset;
  cover_asset?: Asset | null;
};

function fileNameFromKey(key: string): string {
  const parts = (key ?? "").split("/");
  return parts[parts.length - 1] || key || "-";
}

export default function DashboardPage() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [videos, setVideos] = useState<ConvertedVideoItem[] | null>(null);
  const [videosError, setVideosError] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?limit=200`)
      .then((data) => setTasks(data))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=12`)
      .then((data) => setVideos(data))
      .catch((e: unknown) => setVideosError(e instanceof Error ? e.message : String(e)));
  }, []);

  const counts = useMemo(() => {
    const out = new Map<string, number>();
    for (const t of tasks ?? []) out.set(t.status, (out.get(t.status) ?? 0) + 1);
    return Array.from(out.entries()).sort((a, b) => b[1] - a[1]);
  }, [tasks]);

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-lg font-semibold">Dashboard</div>
            <div className="text-sm text-slate-600">快速查看任务状态与入口</div>
          </div>
          <Link to="/tasks/new" className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
            新建任务
          </Link>
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="mb-2 text-sm font-semibold">最近任务（200 条内）状态统计</div>
        {error ? <div className="text-sm text-rose-700">{error}</div> : null}
        {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {tasks ? (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
            {counts.map(([status, n]) => (
              <div key={status} className="rounded border p-3">
                <div className="text-xs text-slate-500">{status}</div>
                <div className="text-2xl font-semibold">{n}</div>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">已转换视频（video_final）</div>
          <Link to="/videos" className="text-sm text-slate-700 hover:underline">
            管理全部 →
          </Link>
        </div>
        <div className="mt-1 text-xs text-slate-500">展示最近 12 条已生成最终视频的任务，可下载/进入详情继续操作。</div>
        {videosError ? <div className="mt-2 text-sm text-rose-700">{videosError}</div> : null}
        {!videos ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {videos ? (
          <div className="mt-3 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Video</th>
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {videos.map((it) => (
                  <tr key={it.final_asset.id} className="border-t">
                    <td className="py-2 pr-3">
                      <div className="font-mono text-xs">{fileNameFromKey(it.final_asset.storage_key)}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <Link to={`/tasks/${it.task.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                        {it.task.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="py-2 pr-3">
                      <StatusBadge status={it.task.status} />
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}/download`}
                        >
                          Download
                        </a>
                        <Link className="rounded border px-2 py-1 text-xs hover:bg-slate-50" to={`/tasks/${it.task.id}`}>
                          Detail
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
                {videos.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-6 text-center text-sm text-slate-500">
                      暂无
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  );
}
