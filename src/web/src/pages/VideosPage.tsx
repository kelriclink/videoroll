import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { Asset, Task } from "../lib/types";
import { formatBulkDeleteSummary, summarizeBulkDeleteResults } from "./videosPage.helpers";

type ConvertedVideoItem = {
  task: Task;
  final_asset: Asset;
  cover_asset?: Asset | null;
  display_title?: string | null;
};

type WorkdirMaintenance = {
  work_dir: string;
  scanned_dirs: number;
  reclaimable_dirs: number;
  total_bytes: number;
  reclaimable_bytes: number;
  deleted_dirs: number;
  deleted_bytes: number;
  deleted_paths: string[];
  errors: string[];
};

function fileNameFromKey(key: string): string {
  const parts = (key ?? "").split("/");
  return parts[parts.length - 1] || key || "-";
}

function formatBytes(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, Number(value || 0));
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 100 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`;
}

function formatWorkdirSummary(result: WorkdirMaintenance, mode: "scan" | "cleanup"): string {
  const parts: string[] = [];
  if (mode === "scan") {
    parts.push(
      `临时目录扫描完成：共 ${result.scanned_dirs} 个目录，占用 ${formatBytes(result.total_bytes)}；可回收 ${result.reclaimable_dirs} 个，预计释放 ${formatBytes(result.reclaimable_bytes)}。`,
    );
  } else {
    parts.push(
      `临时目录清理完成：删除 ${result.deleted_dirs} 个目录，释放 ${formatBytes(result.deleted_bytes)}；当前仍可回收 ${result.reclaimable_dirs} 个，预计释放 ${formatBytes(result.reclaimable_bytes)}。`,
    );
  }
  if (result.errors.length > 0) {
    const preview = result.errors.slice(0, 2).join("；");
    const suffix = result.errors.length > 2 ? `；其余 ${result.errors.length - 2} 条错误请查看日志。` : "";
    parts.push(`错误：${preview}${suffix}`);
  }
  parts.push(`工作目录：${result.work_dir}`);
  return parts.join(" ");
}

export default function VideosPage() {
  const [items, setItems] = useState<ConvertedVideoItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteSummary, setDeleteSummary] = useState<string | null>(null);
  const [maintenanceSummary, setMaintenanceSummary] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  async function refresh() {
    setError(null);
    try {
      const data = await fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=200`);
      setItems(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!items) return;
    const allowed = new Set(items.map((it) => it.final_asset.id));
    setSelected((prev) => {
      const next = new Set(Array.from(prev).filter((id) => allowed.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [items]);

  const selectedCount = selected.size;
  const allSelected = useMemo(
    () => (items && items.length > 0 ? items.every((it) => selected.has(it.final_asset.id)) : false),
    [items, selected],
  );
  const someSelected = useMemo(
    () => (items && items.length > 0 ? items.some((it) => selected.has(it.final_asset.id)) : false),
    [items, selected],
  );

  useEffect(() => {
    if (!selectAllRef.current) return;
    selectAllRef.current.indeterminate = !allSelected && someSelected;
  }, [allSelected, someSelected]);

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-lg font-semibold">Videos</div>
            <div className="text-sm text-slate-600">已经转换完成（存在 video_final）的任务列表</div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
              刷新
            </button>
            <button
              disabled={busy}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
              onClick={async () => {
                setBusy(true);
                setError(null);
                setMaintenanceSummary(null);
                try {
                  const result = await fetchJson<WorkdirMaintenance>(`${ORCHESTRATOR_URL}/maintenance/workdir`);
                  setMaintenanceSummary(formatWorkdirSummary(result, "scan"));
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              扫描临时目录
            </button>
            <button
              disabled={busy}
              className="rounded border border-amber-300 px-3 py-2 text-sm text-amber-800 hover:bg-amber-50 disabled:opacity-50"
              onClick={async () => {
                if (!confirm("确定清理可安全回收的临时目录吗？不会删除运行中的任务目录。")) return;
                setBusy(true);
                setError(null);
                setMaintenanceSummary(null);
                try {
                  const result = await fetchJson<WorkdirMaintenance>(`${ORCHESTRATOR_URL}/maintenance/workdir/cleanup`, {
                    method: "POST",
                  });
                  setMaintenanceSummary(formatWorkdirSummary(result, "cleanup"));
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              清理临时目录
            </button>
            <label className="flex items-center gap-2 rounded border px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
              <input
                ref={selectAllRef}
                type="checkbox"
                checked={allSelected}
                disabled={busy || !items || items.length === 0}
                onChange={(e) => {
                  const checked = e.target.checked;
                  setSelected(() => {
                    if (!items) return new Set();
                    return checked ? new Set(items.map((it) => it.final_asset.id)) : new Set();
                  });
                }}
              />
              全选
            </label>
            <button
              disabled={busy || selectedCount === 0}
              className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
              onClick={async () => {
                if (!items || selectedCount === 0) return;
                if (!confirm(`确定删除选中的 ${selectedCount} 个最终视频（video_final）吗？`)) return;
                setBusy(true);
                setError(null);
                setDeleteSummary(null);
                try {
                  const targets = items
                    .filter((it) => selected.has(it.final_asset.id))
                    .map((it) => ({
                      assetId: it.final_asset.id,
                      label: it.display_title?.trim() || fileNameFromKey(it.final_asset.storage_key),
                      taskId: it.task.id,
                    }));
                  const results = await Promise.allSettled(
                    targets.map((target) =>
                      fetchJson(`${ORCHESTRATOR_URL}/tasks/${target.taskId}/assets/${target.assetId}`, {
                        method: "DELETE",
                      }),
                    ),
                  );
                  const summary = summarizeBulkDeleteResults(
                    targets.map(({ assetId, label }) => ({ assetId, label })),
                    results,
                  );
                  setDeleteSummary(formatBulkDeleteSummary(summary));
                  setSelected(new Set(summary.failures.map((failure) => failure.assetId)));
                  await refresh();
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              删除选中{selectedCount ? ` (${selectedCount})` : ""}
            </button>
            <Link to="/tasks/new" className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
              新建任务
            </Link>
          </div>
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
        {deleteSummary ? <div className="mt-3 text-sm text-slate-700">{deleteSummary}</div> : null}
        {maintenanceSummary ? <div className="mt-3 text-sm text-slate-700">{maintenanceSummary}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        {!items ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {items ? (
          <div className="overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Select</th>
                  <th className="py-2 pr-3">Video</th>
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2 pr-3">Created</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <tr key={it.final_asset.id} className="border-t">
                    <td className="py-2 pr-3">
                      <input
                        type="checkbox"
                        checked={selected.has(it.final_asset.id)}
                        disabled={busy}
                        onChange={(e) => {
                          const checked = e.target.checked;
                          setSelected((prev) => {
                            const next = new Set(prev);
                            if (checked) next.add(it.final_asset.id);
                            else next.delete(it.final_asset.id);
                            return next;
                          });
                        }}
                      />
                    </td>
                    <td className="py-2 pr-3">
                      <div className="text-sm font-semibold text-slate-900">{it.display_title?.trim() || fileNameFromKey(it.final_asset.storage_key)}</div>
                      <div className="font-mono text-[11px] text-slate-600">{fileNameFromKey(it.final_asset.storage_key)}</div>
                      <div className="font-mono text-[11px] text-slate-500">{it.final_asset.storage_key}</div>
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
                      <div className="text-xs text-slate-500">{it.task.source_type}</div>
                      <div className="max-w-[28rem] truncate text-xs text-slate-700">{it.task.source_url ?? "-"}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="text-xs text-slate-700">{new Date(it.final_asset.created_at).toLocaleString()}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}/stream`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Play
                        </a>
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}/download`}
                        >
                          Download
                        </a>
                        {it.cover_asset ? (
                          <a
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                            href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.cover_asset.id}/download`}
                          >
                            Cover
                          </a>
                        ) : null}
                        <button
                          disabled={busy}
                          className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                          onClick={async () => {
                            if (!confirm("确定删除这个最终视频（video_final）吗？")) return;
                            setBusy(true);
                            setError(null);
                            setDeleteSummary(null);
                            try {
                              await fetchJson(`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}`, {
                                method: "DELETE",
                              });
                              await refresh();
                            } catch (e: unknown) {
                              setError(e instanceof Error ? e.message : String(e));
                            } finally {
                              setBusy(false);
                            }
                          }}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {items.length === 0 ? <div className="py-6 text-center text-sm text-slate-500">暂无</div> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
