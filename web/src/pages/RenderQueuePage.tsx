import { Link } from "react-router-dom";
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type RenderJob = {
  id: string;
  task_id: string;
  subtitle_job_id?: string | null;
  status: string;
  progress: number;
  retry_count: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

type RenderQueue = {
  settings: { max_concurrency: number };
  running_count: number;
  queued_count: number;
  jobs: RenderJob[];
};

export default function RenderQueuePage() {
  const [queue, setQueue] = useState<RenderQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [maxConcText, setMaxConcText] = useState("1");
  const [maxConcDirty, setMaxConcDirty] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const q = await fetchJson<RenderQueue>(`${SUBTITLE_SERVICE_URL}/subtitle/render_queue`);
      setQueue(q);
      if (!maxConcDirty) setMaxConcText(String(q?.settings?.max_concurrency ?? 1));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [maxConcDirty]);

  useEffect(() => {
    refresh();
  }, []);

  const shouldPoll = useMemo(() => (queue?.jobs ?? []).some((j) => j.status === "queued" || j.status === "running"), [queue]);

  useEffect(() => {
    if (!shouldPoll) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      if (cancelled) return;
      await refresh();
      if (cancelled) return;
      timer = window.setTimeout(tick, 1500);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [shouldPoll, refresh]);

  async function saveMaxConcurrency() {
    setBusy(true);
    setError(null);
    try {
      const raw = maxConcText.trim();
      if (!raw) throw new Error("max_concurrency 不能为空");
      const n = Number(raw);
      if (!Number.isFinite(n) || !Number.isInteger(n)) throw new Error("max_concurrency 必须是整数");
      if (n < 0 || n > 32) throw new Error("max_concurrency 范围：0..32（0=暂停）");
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/render_queue/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_concurrency: n }),
      });
      setMaxConcDirty(false);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Queue · Render (ffmpeg)</div>
        <div className="mt-1 text-sm text-slate-600">
          用于限制 “压制硬字幕 / 封装软字幕” 的并发数；ASR/翻译不受该队列影响。
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-semibold">Settings</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">max_concurrency（0=暂停）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={maxConcText}
              onChange={(e) => {
                setMaxConcText(e.target.value);
                setMaxConcDirty(true);
              }}
            />
          </label>
          <div className="flex items-end">
            <button
              disabled={busy}
              className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
              onClick={saveMaxConcurrency}
            >
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
        </div>

        <div className="mt-3 text-xs text-slate-600">
          running: {queue?.running_count ?? 0} · queued: {queue?.queued_count ?? 0}
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Current Queue</div>
        {!queue ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {queue && (queue.jobs ?? []).length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
        {queue && (queue.jobs ?? []).length > 0 ? (
          <div className="mt-2 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Progress</th>
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">Subtitle Job</th>
                  <th className="py-2 pr-3">Retry</th>
                  <th className="py-2 pr-3">Updated</th>
                  <th className="py-2 pr-3">Error</th>
                </tr>
              </thead>
              <tbody>
                {(queue.jobs ?? []).map((j) => (
                  <tr key={j.id} className="border-t">
                    <td className="py-2 pr-3 font-mono text-xs">{j.status}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{j.progress}%</td>
                    <td className="py-2 pr-3">
                      <Link className="font-mono text-xs text-slate-900 hover:underline" to={`/tasks/${j.task_id}`}>
                        {j.task_id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{j.subtitle_job_id ? j.subtitle_job_id.slice(0, 8) : "-"}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{j.retry_count ?? 0}</td>
                    <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      {j.error_message ? (
                        <div className="max-w-[36rem] truncate text-xs text-rose-700" title={j.error_message}>
                          {j.error_message}
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400">-</span>
                      )}
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
