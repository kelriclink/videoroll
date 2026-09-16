import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { liveApi } from "../../api/live";
import LiveView from "./LiveView";

export default function LivePage() {
  const [legacyEnabled, setLegacyEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    liveApi.legacyStatus()
      .then((status) => {
        if (!cancelled) setLegacyEnabled(status.enabled);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : String(cause));
          setLegacyEnabled(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (legacyEnabled === null) {
    return <div className="text-sm text-slate-500">正在检查旧直播系统状态…</div>;
  }

  if (!legacyEnabled) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 p-5 text-slate-900 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-slate-100">
        <div className="text-lg font-semibold">旧直播系统已停用</div>
        <div className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          直播播控已经迁移到 ffplayout。请使用新的播控中心；旧系统仅保留用于紧急回退。
        </div>
        {error ? <div className="mt-2 text-xs text-rose-600 dark:text-rose-300">状态检查失败：{error}</div> : null}
        <Link
          to="/playout"
          className="mt-4 inline-block rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-950 dark:hover:bg-white"
        >
          打开播控中心
        </Link>
      </div>
    );
  }

  return <LiveView />;
}
