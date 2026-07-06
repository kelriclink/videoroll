import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { useConfirm, useToast } from "../components/feedbackContext";
import { Button, DataTable, EmptyState, MoreMenu, PageHeader, PaginationControls, Section, TableToolbar, menuItemClass } from "../components/ui";
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

const PAGE_SIZE = 25;

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

function matchesVideoSearch(item: ConvertedVideoItem, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    item.task.id,
    item.task.status,
    item.task.source_type,
    item.task.source_url,
    item.display_title,
    item.final_asset.storage_key,
    item.cover_asset?.storage_key,
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(q));
}

export default function VideosPage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [items, setItems] = useState<ConvertedVideoItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteSummary, setDeleteSummary] = useState<string | null>(null);
  const [maintenanceSummary, setMaintenanceSummary] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [searchText, setSearchText] = useState("");
  const [page, setPage] = useState(0);
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async (opts?: { silent?: boolean }) => {
    if (opts?.silent) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const data = await fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=200`);
      setItems(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!items) return;
    const allowed = new Set(items.map((it) => it.final_asset.id));
    setSelected((prev) => {
      const next = new Set(Array.from(prev).filter((id) => allowed.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [items]);

  useEffect(() => {
    setPage(0);
  }, [searchText]);

  const filteredItems = useMemo(() => (items ?? []).filter((item) => matchesVideoSearch(item, searchText)), [items, searchText]);
  const pageItems = useMemo(() => filteredItems.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE), [filteredItems, page]);
  const selectedCount = selected.size;
  const allFilteredSelected = useMemo(
    () => (filteredItems.length > 0 ? filteredItems.every((it) => selected.has(it.final_asset.id)) : false),
    [filteredItems, selected],
  );
  const someFilteredSelected = useMemo(
    () => (filteredItems.length > 0 ? filteredItems.some((it) => selected.has(it.final_asset.id)) : false),
    [filteredItems, selected],
  );

  useEffect(() => {
    if (!selectAllRef.current) return;
    selectAllRef.current.indeterminate = !allFilteredSelected && someFilteredSelected;
  }, [allFilteredSelected, someFilteredSelected]);

  async function scanWorkdir() {
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
  }

  async function cleanupWorkdir() {
    const ok = await confirm({
      title: "清理临时目录",
      message: "确定清理可安全回收的临时目录吗？不会删除运行中的任务目录。",
      confirmLabel: "清理",
      tone: "warning",
    });
    if (!ok) return;
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
  }

  async function deleteVideo(item: ConvertedVideoItem) {
    const ok = await confirm({
      title: "删除最终视频",
      message: "确定删除这个最终视频（video_final）吗？",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    setDeleteSummary(null);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/tasks/${item.task.id}/assets/${item.final_asset.id}`, {
        method: "DELETE",
      });
      toast({ kind: "success", title: "已删除最终视频" });
      await refresh({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSelectedVideos() {
    if (!items || selectedCount === 0) return;
    const ok = await confirm({
      title: "删除最终视频",
      message: `确定删除选中的 ${selectedCount} 个最终视频（video_final）吗？`,
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
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
      const message = formatBulkDeleteSummary(summary);
      setDeleteSummary(message);
      toast({
        kind: summary.failureCount === 0 ? "success" : "warning",
        title: "删除操作完成",
        message,
      });
      setSelected(new Set(summary.failures.map((failure) => failure.assetId)));
      await refresh({ silent: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function setAllFilteredSelected(checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) {
        if (checked) next.add(item.final_asset.id);
        else next.delete(item.final_asset.id);
      }
      return next;
    });
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="视频成品"
        description="管理已经转换完成的最终视频、封面和存储清理。"
        actions={
          <Link to="/tasks/new" className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
            新建任务
          </Link>
        }
      />

      <Section>
        <TableToolbar
          title="最终视频"
          description="最多加载最近 200 条已生成 video_final 的任务。"
          meta={
            items
              ? `已加载 ${items.length} 条，当前显示 ${filteredItems.length} 条${refreshing ? "，刷新中..." : ""}`
              : loading
                ? "加载中..."
                : undefined
          }
          actions={
            <>
              {selectedCount > 0 ? (
                <Button disabled={busy} tone="danger" onClick={deleteSelectedVideos}>
                  删除选中 ({selectedCount})
                </Button>
              ) : null}
              <Button disabled={refreshing || loading} onClick={() => refresh({ silent: true })}>
                {refreshing ? "刷新中..." : "刷新"}
              </Button>
              <MoreMenu label="维护操作">
                <button className={menuItemClass} disabled={busy} onClick={scanWorkdir}>
                  扫描临时目录
                </button>
                <button className={menuItemClass} disabled={busy} onClick={cleanupWorkdir}>
                  清理临时目录
                </button>
              </MoreMenu>
            </>
          }
          filters={
            <>
              <input
                className="vr-input w-full lg:w-80"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="搜索标题、文件名、来源链接、任务 ID"
              />
              <label className="flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                <input
                  ref={selectAllRef}
                  type="checkbox"
                  checked={allFilteredSelected}
                  disabled={busy || filteredItems.length === 0}
                  onChange={(e) => setAllFilteredSelected(e.target.checked)}
                />
                选中当前筛选
              </label>
            </>
          }
        />

        {error ? <div className="mt-3 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
        {deleteSummary ? <div className="mt-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">{deleteSummary}</div> : null}
        {maintenanceSummary ? <div className="mt-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">{maintenanceSummary}</div> : null}
        {loading && !items ? <div className="mt-3 text-sm text-slate-500">加载中...</div> : null}
        {items ? (
          <>
            {filteredItems.length === 0 ? (
              <EmptyState>没有匹配的视频</EmptyState>
            ) : (
              <DataTable wrapClassName="mt-3">
                <thead>
                  <tr>
                    <th className="w-10">选中</th>
                    <th>视频</th>
                    <th className="w-24">任务</th>
                    <th className="w-36">状态</th>
                    <th>来源</th>
                    <th className="w-44">生成时间</th>
                    <th className="w-36 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {pageItems.map((item) => (
                    <tr key={item.final_asset.id}>
                      <td>
                        <input
                          type="checkbox"
                          checked={selected.has(item.final_asset.id)}
                          disabled={busy}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            setSelected((prev) => {
                              const next = new Set(prev);
                              if (checked) next.add(item.final_asset.id);
                              else next.delete(item.final_asset.id);
                              return next;
                            });
                          }}
                        />
                      </td>
                      <td>
                        <div className="max-w-[32rem] truncate text-sm font-semibold text-slate-900">
                          {item.display_title?.trim() || fileNameFromKey(item.final_asset.storage_key)}
                        </div>
                        <div className="max-w-[32rem] truncate font-mono text-[11px] text-slate-600">{fileNameFromKey(item.final_asset.storage_key)}</div>
                        <div className="max-w-[32rem] truncate font-mono text-[11px] text-slate-500">{item.final_asset.storage_key}</div>
                      </td>
                      <td>
                        <Link to={`/tasks/${item.task.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                          {item.task.id.slice(0, 8)}
                        </Link>
                      </td>
                      <td>
                        <StatusBadge status={item.task.status} />
                      </td>
                      <td>
                        <div className="text-xs text-slate-500">{item.task.source_type}</div>
                        <div className="max-w-[28rem] truncate text-xs text-slate-700">{item.task.source_url ?? "-"}</div>
                      </td>
                      <td>
                        <div className="text-xs text-slate-700">{new Date(item.final_asset.created_at).toLocaleString()}</div>
                      </td>
                      <td>
                        <div className="flex items-center justify-end gap-2">
                          <a
                            className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-800 hover:bg-slate-50"
                            href={`${ORCHESTRATOR_URL}/tasks/${item.task.id}/assets/${item.final_asset.id}/stream`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            播放
                          </a>
                          <MoreMenu>
                            <a
                              className={menuItemClass}
                              href={`${ORCHESTRATOR_URL}/tasks/${item.task.id}/assets/${item.final_asset.id}/download`}
                            >
                              下载视频
                            </a>
                            {item.cover_asset ? (
                              <a
                                className={menuItemClass}
                                href={`${ORCHESTRATOR_URL}/tasks/${item.task.id}/assets/${item.cover_asset.id}/download`}
                              >
                                下载封面
                              </a>
                            ) : null}
                            <Link className={menuItemClass} to={`/tasks/${item.task.id}`}>
                              查看任务
                            </Link>
                            <button className={`${menuItemClass} text-rose-700`} disabled={busy} onClick={() => deleteVideo(item)}>
                              删除视频
                            </button>
                          </MoreMenu>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
            <PaginationControls
              page={page}
              pageSize={PAGE_SIZE}
              totalItems={filteredItems.length}
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
