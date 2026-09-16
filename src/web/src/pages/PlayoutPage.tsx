import { useState } from "react";
import { Button, PageHeader } from "../components/ui";
import { FFPLAYOUT_URL } from "../lib/urls";

export default function PlayoutPage() {
  const [loading, setLoading] = useState(Boolean(FFPLAYOUT_URL));
  const [error, setError] = useState<string | null>(null);

  function openInNewWindow() {
    if (!FFPLAYOUT_URL) return;
    window.open(FFPLAYOUT_URL, "ffplayout", "noopener,noreferrer");
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-hidden">
      <PageHeader
        title="播控中心"
        description="ffplayout 独立播控系统：Player、Media、Playlist、Logging 与 Configure。"
        actions={
          <Button disabled={!FFPLAYOUT_URL} onClick={openInNewWindow}>
            新窗口打开
          </Button>
        }
      />

      {!FFPLAYOUT_URL ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          尚未配置 ffplayout 地址，请设置构建变量 <code>VITE_FFPLAYOUT_URL</code>。
        </div>
      ) : null}

      {error ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          无法加载 ffplayout：{error}
        </div>
      ) : null}

      {FFPLAYOUT_URL ? (
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-md border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
          {loading ? <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90 text-sm text-slate-500 dark:bg-slate-900/90">正在加载 ffplayout…</div> : null}
          <iframe
            src={FFPLAYOUT_URL}
            title="ffplayout 播控中心"
            allow="autoplay; fullscreen"
            allowFullScreen
            referrerPolicy="no-referrer"
            className="h-full min-h-0 w-full border-0"
            onLoad={() => {
              setLoading(false);
              setError(null);
            }}
            onError={() => {
              setLoading(false);
              setError("请检查 PLAYOUT_HOST 反代和 ffplayout 容器状态。");
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
