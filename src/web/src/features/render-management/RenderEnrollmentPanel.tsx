import type { CreatedEnrollment, RenderConnection } from "../../api/renderManagement";
import { Button, InlineNotice } from "../../components/ui";

export function RenderEnrollmentPanel({
  connection,
  createdEnrollment,
  enrollmentLabel,
  serverUrl,
  busy,
  onEnrollmentLabelChange,
  onServerUrlChange,
  onSaveServerUrl,
  onCreateEnrollment,
}: {
  connection: RenderConnection | null;
  createdEnrollment: CreatedEnrollment | null;
  enrollmentLabel: string;
  serverUrl: string;
  busy: string | null;
  onEnrollmentLabelChange: (value: string) => void;
  onServerUrlChange: (value: string) => void;
  onSaveServerUrl: () => void;
  onCreateEnrollment: () => void;
}) {
  return (
    <details className="vr-section">
      <summary className="cursor-pointer text-sm font-semibold text-slate-950 dark:text-white">节点接入与恢复</summary>
      <div className="mt-1 text-xs text-slate-500">
        首次接入节点、服务端记录丢失或需要重新配对时使用；日常渲染无需打开。
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
        <label>
          <div className="mb-1 text-xs text-slate-500">服务器地址</div>
          <input
            value={serverUrl}
            onChange={(event) => onServerUrlChange(event.target.value)}
            placeholder="https://video.example.com"
            className="vr-input w-full"
          />
        </label>
        <div className="flex items-end">
          <Button
            disabled={busy === "connection" || !serverUrl.trim() || serverUrl.trim() === connection?.server_url}
            onClick={onSaveServerUrl}
          >
            保存地址
          </Button>
        </div>
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px_auto]">
        <div className="self-end pb-2 text-xs text-slate-500">
          节点接口：{connection ? `${connection.server_url}${connection.worker_api_path}` : "—"}
        </div>
        <label>
          <div className="mb-1 text-xs text-slate-500">节点备注</div>
          <input
            value={enrollmentLabel}
            onChange={(event) => onEnrollmentLabelChange(event.target.value)}
            placeholder="例如：A380 渲染机"
            className="vr-input w-full"
          />
        </label>
        <div className="flex items-end">
          <Button tone="primary" disabled={busy === "enrollment"} onClick={onCreateEnrollment}>
            生成一次性配对凭据
          </Button>
        </div>
      </div>

      {createdEnrollment ? (
        <InlineNotice className="mt-4" title="一次性配对凭据" tone="warning">
          <div className="break-all rounded bg-white px-3 py-2 font-mono text-xs text-slate-900 dark:bg-slate-900 dark:text-slate-100">
            {createdEnrollment.token}
          </div>
          <div className="mt-2 text-xs">有效期 30 分钟，成功配对后立即失效。不要把它当作长期节点凭据保存。</div>
        </InlineNotice>
      ) : null}
    </details>
  );
}
