import { useState } from "react";
import { Link } from "react-router-dom";
import { Button, InlineNotice, PageHeader, TechnicalDetails } from "../components/ui";
import { HATCHET_DASHBOARD_URL } from "../lib/urls";

export default function WorkflowCenterPage() {
  const [loading, setLoading] = useState(Boolean(HATCHET_DASHBOARD_URL));
  const [error, setError] = useState<string | null>(null);

  function openInNewWindow() {
    if (!HATCHET_DASHBOARD_URL) return;
    window.open(HATCHET_DASHBOARD_URL, "hatchet-dashboard", "noopener,noreferrer");
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-hidden">
      <PageHeader
        title="工作流诊断（高级）"
        description="用于排查执行器、工作流事件和底层重试。日常任务操作请使用“任务”和“处理队列”页面。"
        actions={
          <>
            <Link
              to="/operations"
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
            >
              返回运维
            </Link>
            <Button disabled={!HATCHET_DASHBOARD_URL} onClick={openInNewWindow}>
              新窗口打开
            </Button>
          </>
        }
      />

      {!HATCHET_DASHBOARD_URL ? (
        <InlineNotice title="工作流诊断控制台未配置" tone="warning">
          生产任务不受影响。需要底层执行诊断时，再配置 Hatchet Dashboard 地址。
          <TechnicalDetails className="mt-2">
            构建 Web 时设置 <code>VITE_HATCHET_DASHBOARD_URL</code>。
          </TechnicalDetails>
        </InlineNotice>
      ) : null}

      {error ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          Hatchet Dashboard 未能正常显示：{error}
        </div>
      ) : null}

      {HATCHET_DASHBOARD_URL ? (
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-md border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900">
          {loading ? (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90 text-sm text-slate-500 dark:bg-slate-900/90">
              正在加载 Hatchet Dashboard…
            </div>
          ) : null}
          <iframe
            src={HATCHET_DASHBOARD_URL}
            title="Hatchet 工作流控制台"
            allow="clipboard-read; clipboard-write; fullscreen"
            allowFullScreen
            className="h-full min-h-0 w-full border-0"
            onLoad={() => {
              setLoading(false);
              setError(null);
            }}
            onError={() => {
              setLoading(false);
              setError("请检查 Hatchet Dashboard 地址、反向代理以及浏览器 frame/CSP 策略。");
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
