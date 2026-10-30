import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { RealtimeEvent, useRealtimeSubscription } from "../lib/realtime";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { reconcileAgentRunStatuses } from "../pages/dashboardPage.helpers";
import {
  buildAgentMonitorGroups,
  type AgentMonitorRun,
} from "./AgentMonitorPanel.helpers";

function textValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function agentTypeLabel(type: string): string {
  if (type === "subtitle_translation_session") return "翻译 Session";
  if (type === "subtitle_translation_batch") return "翻译 Batch";
  if (type === "subtitle_translation_thinking") return "翻译思考";
  if (type === "rag_master") return "RAG Master";
  if (type === "rag_term_research") return "RAG 子 Agent";
  return type || "Agent";
}

function runTitle(run: AgentMonitorRun): string {
  return run.term?.trim() || run.query?.trim() || "Agent " + run.id.slice(0, 8);
}

function latestAction(run: AgentMonitorRun): string {
  const steps = run.steps ?? [];
  const step = steps[steps.length - 1];
  if (!step) return "";
  return textValue(step.action) || textValue(step.tool_name);
}

function runningDescendants(
  run: AgentMonitorRun,
  childrenByParent: Map<string, AgentMonitorRun[]>,
): number {
  let total = 0;
  const visited = new Set<string>();
  const visit = (parent: AgentMonitorRun) => {
    if (visited.has(parent.id)) return;
    visited.add(parent.id);
    for (const child of childrenByParent.get(parent.id) ?? []) {
      if (String(child.status || "").toLowerCase() === "running") total += 1;
      visit(child);
    }
  };
  visit(run);
  return total;
}

function statusMeta(run: AgentMonitorRun, activeDescendants: number) {
  const status = String(run.status || "").toLowerCase();
  if (status === "running" || activeDescendants > 0) {
    return {
      label: status === "running" ? "运行中" : "等待分支",
      dot: "bg-sky-500",
      text: "text-sky-700 dark:text-sky-300",
      active: true,
    };
  }
  if (status === "succeeded") {
    return {
      label: "已完成",
      dot: "bg-emerald-500",
      text: "text-emerald-700 dark:text-emerald-300",
      active: false,
    };
  }
  if (status === "failed") {
    return {
      label: "失败",
      dot: "bg-rose-500",
      text: "text-rose-700 dark:text-rose-300",
      active: false,
    };
  }
  if (status === "interrupted" || status === "skipped") {
    return {
      label: "已中断",
      dot: "bg-amber-500",
      text: "text-amber-700 dark:text-amber-300",
      active: false,
    };
  }
  return {
    label: run.status || "未知",
    dot: "bg-slate-400",
    text: "text-slate-500",
    active: false,
  };
}

function sessionProgress(
  run: AgentMonitorRun,
  childrenByParent: Map<string, AgentMonitorRun[]>,
) {
  if (run.agent_type !== "subtitle_translation_session") return null;
  const children = childrenByParent.get(run.id) ?? [];
  const batches = children.filter((child) => child.agent_type === "subtitle_translation_batch");
  const total = numberValue(run.result?.total_segments) ?? 0;
  const completed = Math.max(
    numberValue(run.result?.completed_segments) ?? 0,
    ...batches.map((batch) => numberValue(batch.result?.completed_segments) ?? 0),
  );
  if (!total) return null;
  return { total, completed: Math.min(total, completed) };
}

function AgentCard({
  run,
  childrenByParent,
  expanded,
  onToggle,
}: {
  run: AgentMonitorRun;
  childrenByParent: Map<string, AgentMonitorRun[]>;
  expanded: boolean;
  onToggle: () => void;
}) {
  const activeDescendants = runningDescendants(run, childrenByParent);
  const meta = statusMeta(run, activeDescendants);
  const action = latestAction(run);
  const progress = sessionProgress(run, childrenByParent);
  const directChildren = childrenByParent.get(run.id) ?? [];
  const percent = progress ? Math.max(0, Math.min(100, progress.completed / progress.total * 100)) : 0;

  return (
    <div className="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950">
      <button type="button" onClick={onToggle} className="block w-full p-3 text-left">
        <div className="flex items-start gap-2">
          <span
            className={[
              "mt-1.5 h-2 w-2 shrink-0 rounded-full",
              meta.dot,
              meta.active ? "animate-pulse" : "",
            ].join(" ")}
          />
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs font-medium text-slate-950 dark:text-white">
              {runTitle(run)}
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px]">
              <span className="truncate text-slate-500">{agentTypeLabel(run.agent_type)}</span>
              <span className={["shrink-0", meta.text].join(" ")}>{meta.label}</span>
            </div>
          </div>
        </div>

        {progress ? (
          <div className="mt-2 pl-4">
            <div className="flex justify-between text-[10px] text-slate-500">
              <span>{progress.completed}/{progress.total} 段</span>
              <span>{Math.round(percent)}%</span>
            </div>
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
              <div className="h-full rounded-full bg-violet-500" style={{ width: String(percent) + "%" }} />
            </div>
          </div>
        ) : null}
      </button>

      {expanded ? (
        <div className="border-t border-slate-100 px-3 py-2.5 text-[11px] dark:border-slate-800">
          <div className="space-y-1 text-slate-500">
            {action ? (
              <div className="truncate">
                <span className="text-slate-400">最近动作：</span>{action}
              </div>
            ) : null}
            <div>
              <span className="text-slate-400">子 Agent：</span>
              {directChildren.length}
              {activeDescendants ? " · " + activeDescendants + " 运行中" : ""}
            </div>
            <div>
              <span className="text-slate-400">更新：</span>
              {new Date(run.updated_at).toLocaleTimeString()}
            </div>
          </div>
          {run.error ? (
            <div className="mt-2 line-clamp-3 text-rose-700 dark:text-rose-300">{run.error}</div>
          ) : null}
          {run.task_id ? (
            <Link
              to={"/tasks/" + run.task_id}
              className="mt-2 inline-flex text-sky-700 hover:underline dark:text-sky-300"
            >
              打开关联任务
            </Link>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function AgentMonitorPanel({
  onRunningCountChange,
  compact = false,
}: {
  onRunningCountChange?: (count: number) => void;
  compact?: boolean;
}) {
  const [runs, setRuns] = useState<AgentMonitorRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const refreshTimerRef = useRef<number | undefined>();

  const load = useCallback(async () => {
    try {
      const data = await fetchJson<AgentMonitorRun[]>(
        ORCHESTRATOR_URL + "/subtitle/agents/runs?limit=100&include_descendants=true",
      );
      setRuns(reconcileAgentRunStatuses(data));
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const scheduleRefresh = useCallback(() => {
    if (refreshTimerRef.current) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = undefined;
      void load();
    }, 800);
  }, [load]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => () => {
    if (refreshTimerRef.current) window.clearTimeout(refreshTimerRef.current);
  }, []);

  const handleRealtime = useCallback((event: RealtimeEvent) => {
    if (
      event.name === "agent_run.started"
      || event.name === "agent_run.resumed"
      || event.name === "agent_run.finished"
      || event.name === "agent_run.step_appended"
    ) {
      scheduleRefresh();
    }
  }, [scheduleRefresh]);

  useRealtimeSubscription(["agents"], handleRealtime, () => void load());

  const groups = useMemo(
    () => buildAgentMonitorGroups(runs ?? []),
    [runs],
  );

  useEffect(() => {
    onRunningCountChange?.(groups.running.length);
  }, [groups.running.length, onRunningCountChange]);

  return (
    <div className={compact ? "space-y-3" : "rounded-xl border border-slate-200 bg-slate-50/70 p-3 dark:border-slate-800 dark:bg-slate-900/40"}>
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-xs font-semibold text-slate-950 dark:text-white">Agent</div>
          <div className="mt-0.5 text-[10px] text-slate-500">实时运行监控</div>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded px-1.5 py-1 text-[10px] text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
        >
          刷新
        </button>
      </div>

      {error ? (
        <div className="rounded-md bg-rose-50 p-2 text-[11px] text-rose-700 dark:bg-rose-950/30 dark:text-rose-300">
          Agent 数据暂时不可用
        </div>
      ) : null}

      {!runs ? <div className="py-4 text-center text-xs text-slate-500">加载中…</div> : null}

      {runs ? (
        <>
          <div>
            <div className="mb-1.5 flex items-center justify-between text-[10px] font-medium text-slate-500">
              <span>正在工作</span>
              <span>{groups.running.length}</span>
            </div>
            <div className="space-y-2">
              {groups.running.length === 0 ? (
                <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-center text-[11px] text-slate-500 dark:border-slate-800">
                  当前没有运行中的 Agent
                </div>
              ) : groups.running.map((run) => (
                <AgentCard
                  key={run.id}
                  run={run}
                  childrenByParent={groups.childrenByParent}
                  expanded={expandedId === run.id}
                  onToggle={() => setExpandedId((current) => current === run.id ? null : run.id)}
                />
              ))}
            </div>
          </div>

          {groups.recent.length ? (
            <details className="border-t border-slate-200 pt-2 dark:border-slate-800">
              <summary className="cursor-pointer text-[10px] font-medium text-slate-500">
                最近完成 · {groups.recent.length}
              </summary>
              <div className="mt-2 space-y-2">
                {groups.recent.map((run) => (
                  <AgentCard
                    key={run.id}
                    run={run}
                    childrenByParent={groups.childrenByParent}
                    expanded={expandedId === run.id}
                    onToggle={() => setExpandedId((current) => current === run.id ? null : run.id)}
                  />
                ))}
              </div>
            </details>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
