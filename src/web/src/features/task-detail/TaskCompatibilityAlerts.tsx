import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../../lib/http";
import { ORCHESTRATOR_URL } from "../../lib/urls";

type TaskAlert = {
  id: string;
  source: string;
  severity: string;
  title: string;
  message: string;
  details?: { reason?: string; pipeline?: string; code?: string };
};

export function TaskCompatibilityAlerts({ taskId }: { taskId: string }) {
  const [alerts, setAlerts] = useState<TaskAlert[]>([]);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void fetchJson<TaskAlert[]>(`${ORCHESTRATOR_URL}/operations/alerts?status=active&task_id=${encodeURIComponent(taskId)}&limit=50`)
        .then((items) => { if (active) setAlerts(items); })
        .catch(() => { /* AlertCenter is noncritical and must not break task detail. */ });
    };
    refresh();
    const interval = window.setInterval(refresh, 30_000);
    return () => { active = false; window.clearInterval(interval); };
  }, [taskId]);
  if (alerts.length === 0) return null;
  return (
    <div role="status" aria-label="任务兼容性与失败提示" className="rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <strong>任务告警 · 兼容性回退 / 失败（{alerts.length}）</strong>
        <Link className="underline" to="/operations">前往告警中心查看及确认</Link>
      </div>
      <ul className="space-y-2">
        {alerts.slice(0, 8).map((alert) => (
          <li key={alert.id} className="border-t border-amber-200 pt-2 first:border-0 first:pt-0">
            <strong>{alert.severity === "critical" ? "严重 · " : "注意 · "}{alert.title}</strong>
            <div>{alert.message}</div>
            {alert.details?.reason ? <div className="mt-1 break-words text-xs opacity-80">原因：{alert.details.reason}</div> : null}
            {alert.details?.pipeline ? <div className="mt-1 break-words text-xs opacity-80">实际渲染路径：{alert.details.pipeline}</div> : null}
          </li>
        ))}
      </ul>
      {alerts.length > 8 ? <p className="mt-2 text-xs">另有 {alerts.length - 8} 条提示，请前往告警中心查看。</p> : null}
    </div>
  );
}
