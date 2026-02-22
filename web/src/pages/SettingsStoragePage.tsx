import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type StorageRetentionSettings = {
  asset_ttl_days: number;
};

export default function SettingsStoragePage() {
  const [settings, setSettings] = useState<StorageRetentionSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
          提示：当 <span className="font-mono">asset_ttl_days &gt; 0</span> 时，后端会按周期清理过期资源（raw/sub/final 等资产文件）。
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
    </div>
  );
}

