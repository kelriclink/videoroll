import { useEffect, useState } from "react";
import { useConfirm, useToast } from "../components/feedbackContext";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type StorageRetentionSettings = {
  asset_ttl_days: number;
};
type StorageResourceCleanupResult = {
  matched_tasks: number;
  matched_assets: number;
  matched_subtitles: number;
  deleted_assets: number;
  deleted_subtitles: number;
  deleted_objects: number;
  pending_objects: number;
};

export default function SettingsStoragePage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [settings, setSettings] = useState<StorageRetentionSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<StorageResourceCleanupResult | null>(null);

  const [assetTtlDays, setAssetTtlDays] = useState(0);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<StorageRetentionSettings>(`${ORCHESTRATOR_URL}/settings/storage`);
      setSettings(s);
      setAssetTtlDays(typeof s.asset_ttl_days === "number" ? s.asset_ttl_days : 0);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Settings · Storage</div>
        <div className="mt-1 text-sm text-slate-600">设置资源自动清理（MinIO/S3）保留时间。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">asset_ttl_days</div>
              <div className="mt-1 font-mono text-sm">{settings.asset_ttl_days}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">自动清理</div>
              <div className="mt-1 text-sm">{settings.asset_ttl_days > 0 ? "enabled" : "disabled"}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">
          提示：<span className="font-mono">asset_ttl_days</span> 控制已发布和永久取消任务的资源保留期；失败任务资源固定在失败 48 小时后清理。任务记录和发布记录会保留。
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">保存配置</div>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">asset_ttl_days（0=永不删除）</div>
            <input
              type="number"
              min={0}
              max={3650}
              className="w-full rounded border px-3 py-2 text-sm"
              value={assetTtlDays}
              onChange={(e) => setAssetTtlDays(parseInt(e.target.value || "0", 10))}
            />
          </label>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/storage`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ asset_ttl_days: assetTtlDays }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            保存
          </button>
        </div>
      </div>

      <div className="rounded border border-rose-200 bg-rose-50 p-4">
        <div className="text-sm font-semibold text-rose-900">立即清理已结束任务资源</div>
        <div className="mt-1 text-sm text-rose-800">
          一键删除所有已发布、失败或永久取消任务的 MinIO/S3 资源文件，包括原视频、成品、字幕、日志和元数据；保留任务、发布和去重记录。已停止且可恢复的任务不会被清理。
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={cleanupBusy}
            className="rounded border border-rose-300 bg-white px-3 py-2 text-sm font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-50"
            onClick={async () => {
              const ok = await confirm({
                title: "清理全部已结束任务资源",
                message: "会永久删除 MinIO/S3 中所有已结束任务的原视频、成品、字幕、日志和元数据，任务记录仍会保留用于去重。此操作不可撤销。",
                confirmLabel: "确认清理",
                tone: "danger",
              });
              if (!ok) return;
              setCleanupBusy(true);
              setError(null);
              try {
                const result = await fetchJson<StorageResourceCleanupResult>(`${ORCHESTRATOR_URL}/maintenance/storage/cleanup-terminal`, {
                  method: "POST",
                });
                setCleanupResult(result);
                toast({ kind: "success", title: "资源清理已完成", message: `删除对象 ${result.deleted_objects} 个。` });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setCleanupBusy(false);
              }
            }}
          >
            {cleanupBusy ? "清理中..." : "一键清理全部已结束任务资源"}
          </button>
          {cleanupResult ? (
            <span className="text-xs text-rose-800">
              已匹配 {cleanupResult.matched_tasks} 个任务，删除 {cleanupResult.deleted_objects} 个对象、{cleanupResult.deleted_assets} 条资产记录。
              {cleanupResult.pending_objects ? ` 仍有 ${cleanupResult.pending_objects} 个对象等待重试。` : ""}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}
