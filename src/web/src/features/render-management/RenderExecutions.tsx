import { Link } from "react-router-dom";
import type { RenderExecution } from "../../api/renderManagement";
import { Button, DataTable, EmptyState, InlineNotice, Metric, Section, TechnicalDetails } from "../../components/ui";
import { renderStageLabel } from "../../lib/executionPresentation";
import { formatRenderDuration, formatRenderFps, formatRenderSpeed, getRenderTelemetry } from "../renderTelemetry";
import { ACTIVE_RENDER_STATES, ago, badgeClass, executionStatusLabel } from "./presentation";

export function RenderExecutions({
  executions,
  selected,
  busy,
  onSelect,
  onClose,
  onAction,
}: {
  executions: RenderExecution[];
  selected: RenderExecution | null;
  busy: string | null;
  onSelect: (execution: RenderExecution) => void;
  onClose: () => void;
  onAction: (execution: RenderExecution, action: "cancel" | "requeue") => void;
}) {
  const selectedTelemetry = selected ? getRenderTelemetry(selected) : null;

  return (
    <>
      <Section>
        <div className="mb-4">
          <div className="text-sm font-semibold text-slate-950 dark:text-white">执行记录</div>
          <div className="mt-1 text-xs text-slate-500">
            每次重新执行都会留下独立记录；调度租约、内部 ID 和原始指标仅在技术详情中显示。
          </div>
        </div>

        {executions.length === 0 ? <EmptyState>暂无渲染执行记录。</EmptyState> : (
          <DataTable>
            <thead>
              <tr>
                <th>状态</th>
                <th>节点</th>
                <th>任务</th>
                <th>进度</th>
                <th>性能</th>
                <th>开始</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {executions.map((execution) => {
                const telemetry = getRenderTelemetry(execution);
                return (
                  <tr key={execution.id} className="cursor-pointer" onClick={() => onSelect(execution)}>
                    <td>
                      <span className={`rounded-full px-2 py-1 text-xs ${badgeClass(execution.state)}`}>
                        {executionStatusLabel(execution.state)}
                      </span>
                    </td>
                    <td>
                      <div>{execution.worker_name ?? "未知节点"}</div>
                      {telemetry.deviceName ? <div className="mt-0.5 text-[11px] text-slate-500">{telemetry.deviceName}</div> : null}
                    </td>
                    <td>
                      {execution.task_id ? (
                        <Link
                          className="text-sky-700 hover:underline dark:text-sky-300"
                          to={`/tasks/${execution.task_id}`}
                          onClick={(event) => event.stopPropagation()}
                        >
                          {execution.task_id.slice(0, 8)}
                        </Link>
                      ) : "—"}
                    </td>
                    <td className="min-w-40">
                      <div className="flex justify-between text-xs">
                        <span>{telemetry.overallProgress}%</span>
                        <span className="text-slate-500">{renderStageLabel(telemetry.stage)}</span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                        <div className="h-full rounded-full bg-sky-500" style={{ width: `${telemetry.overallProgress}%` }} />
                      </div>
                    </td>
                    <td className="text-xs">
                      <div>{formatRenderFps(telemetry.fps)} · {formatRenderSpeed(telemetry.speed)}</div>
                      <div className="mt-0.5 text-slate-500">预计 {formatRenderDuration(telemetry.etaSeconds)}</div>
                    </td>
                    <td>{ago(execution.started_at)}</td>
                    <td>
                      <div className="flex gap-1" onClick={(event) => event.stopPropagation()}>
                        {ACTIVE_RENDER_STATES.has(execution.state) ? (
                          <Button
                            size="xs"
                            tone="danger"
                            disabled={busy === execution.id}
                            onClick={() => onAction(execution, "cancel")}
                          >
                            取消
                          </Button>
                        ) : null}
                        <Button
                          size="xs"
                          disabled={busy === execution.id || execution.state === "succeeded"}
                          onClick={() => onAction(execution, "requeue")}
                        >
                          重新执行
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </DataTable>
        )}
      </Section>

      {selected ? (
        <Section>
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950 dark:text-white">执行详情 · 第 {selected.attempt} 次</div>
              <div className="mt-1 text-xs text-slate-500">
                {selected.worker_name ?? "未知节点"} · {executionStatusLabel(selected.state)}
              </div>
            </div>
            <Button size="xs" onClick={onClose}>关闭</Button>
          </div>

          {selectedTelemetry ? (
            <div className="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Metric label="进度" value={`${selectedTelemetry.overallProgress}%`} />
              <Metric label="FPS" value={formatRenderFps(selectedTelemetry.fps)} />
              <Metric label="实时倍速" value={formatRenderSpeed(selectedTelemetry.speed)} />
              <Metric label="预计剩余" value={formatRenderDuration(selectedTelemetry.etaSeconds)} />
            </div>
          ) : null}

          {selected.error_message ? (
            <InlineNotice className="mt-4" title="本次执行失败" tone="danger">
              渲染执行没有完成。原始错误保留在技术详情中。
            </InlineNotice>
          ) : null}

          <TechnicalDetails className="mt-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div><span className="text-slate-500">执行 ID：</span><span className="font-mono">{selected.id}</span></div>
              <div><span className="text-slate-500">内部任务状态：</span>{selected.job_status ?? "—"}</div>
              <div><span className="text-slate-500">调度租约：</span>{ago(selected.lease_until)}</div>
              <div><span className="text-slate-500">编码路径：</span>{selectedTelemetry?.pipeline ?? "—"}</div>
            </div>
            {selected.error_message ? (
              <pre className="mt-3 max-h-44 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-xs text-slate-100">
                {selected.error_message}
              </pre>
            ) : null}
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <div>
                <div className="mb-1 font-medium">指标</div>
                <pre className="max-h-52 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">
                  {JSON.stringify(selected.metrics, null, 2)}
                </pre>
              </div>
              <div>
                <div className="mb-1 font-medium">日志尾部</div>
                <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-3 text-xs text-slate-100">
                  {selected.log_tail || "暂无日志"}
                </pre>
              </div>
            </div>
          </TechnicalDetails>
        </Section>
      ) : null}
    </>
  );
}
