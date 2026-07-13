import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import StatusBadge from "../components/StatusBadge";
import { Asset, Task } from "../lib/types";
import { PageHeader } from "../components/ui";

type ConvertedVideoItem = {
  task: Task;
  final_asset: Asset;
  cover_asset?: Asset | null;
};

type ResourceMemory = {
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  percent?: number | null;
};

type ResourceSnapshot = {
  sampled_at: string;
  cpu: {
    percent?: number | null;
    cores: number;
    load_average?: number[] | null;
  };
  memory: ResourceMemory;
  cgroup_memory?: ResourceMemory | null;
  intel_gpu?: {
    enabled: boolean;
    checked: boolean;
    available: boolean;
    render_device: string;
    model_name?: string | null;
    driver?: string | null;
    usage_supported: boolean;
    usage_percent?: number | null;
    detail?: string | null;
    engines: Array<{ name: string; percent?: number | null }>;
  } | null;
};

type AgentRun = {
  id: string;
  agent_type: string;
  status: string;
  term: string;
  domain: string;
  target_lang: string;
  task_id?: string | null;
  subtitle_job_id?: string | null;
  query: string;
  steps: Array<Record<string, unknown>>;
  result: Record<string, unknown>;
  error: string;
  knowledge_item_id?: string | null;
  parent_agent_run_id?: string | null;
  started_at: string;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
};

function fileNameFromKey(key: string): string {
  const parts = (key ?? "").split("/");
  return parts[parts.length - 1] || key || "-";
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

export function knowledgeItemHref(itemId: string): string {
  return `/knowledge?${new URLSearchParams({ item: itemId }).toString()}`;
}

function clampPercent(value?: number | null): number {
  if (!Number.isFinite(Number(value))) return 0;
  return Math.max(0, Math.min(100, Number(value)));
}

function formatPercent(value?: number | null): string {
  if (!Number.isFinite(Number(value))) return "-";
  return `${clampPercent(value).toFixed(1)}%`;
}

function formatBytes(value?: number | null): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, Number(value || 0));
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function formatDurationMs(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n < 1000) return `${Math.max(0, Math.round(n))} ms`;
  return `${(n / 1000).toFixed(1)} s`;
}

type AgentDisplayStatus = {
  label: string;
  tone: "running" | "success" | "warning" | "failed" | "idle";
  title: string;
};

function agentStatusClass(status: AgentDisplayStatus["tone"] | string): string {
  if (status === "running" || status === "running_children") return "border-sky-200 bg-sky-50 text-sky-800";
  if (status === "success" || status === "succeeded") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "warning" || status === "skipped" || status === "partial") return "border-amber-200 bg-amber-50 text-amber-800";
  if (status === "failed" || status === "error") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function agentFailureCategory(run: AgentRun): string {
  return textValue(run.result?.failure_category);
}

function agentDisplayStatus(run: AgentRun, childrenRuns: AgentRun[] = []): AgentDisplayStatus {
  const status = (run.status || "").toLowerCase();
  const childStatuses = childrenRuns.map((child) => agentDisplayStatus(child));
  const runningChildren = childStatuses.filter((child) => child.tone === "running").length;
  const failedChildren = childStatuses.filter((child) => child.tone === "failed").length;
  const warningChildren = childStatuses.filter((child) => child.tone === "warning").length;
  const failureCategory = agentFailureCategory(run);
  const knowledgeStatus = textValue(run.result?.knowledge_status);
  const resultError = textValue(run.result?.error);
  const hasError = Boolean(run.error) || Boolean(resultError) || status === "failed";

  if (hasError) {
    return { label: "failed", tone: "failed", title: run.error || resultError || failureCategory || "Agent failed" };
  }
  if (status === "running" || runningChildren > 0) {
    return {
      label: status === "running" ? "running" : "waiting",
      tone: "running",
      title: runningChildren > 0 ? `${runningChildren} 个子 Agent 仍在运行` : "Agent is running",
    };
  }
  if (failedChildren > 0) {
    return { label: "partial", tone: "warning", title: `${failedChildren} 个子 Agent 失败，主流程已继续` };
  }
  if (status === "skipped") {
    return { label: "skipped", tone: "warning", title: failureCategory || knowledgeStatus || "Agent skipped writing knowledge" };
  }
  if (failureCategory || knowledgeStatus === "not_written" || knowledgeStatus === "context_only" || warningChildren > 0) {
    return {
      label: "partial",
      tone: "warning",
      title: failureCategory || knowledgeStatus || `${warningChildren} 个子 Agent 未写入长期知识库`,
    };
  }
  if (status === "succeeded") {
    return { label: "success", tone: "success", title: "Agent succeeded" };
  }
  return { label: run.status || "unknown", tone: "idle", title: run.status || "unknown" };
}

function stepToneClass(kind: unknown, action: unknown): string {
  const k = String(kind || "");
  const a = String(action || "");
  if (a.includes("failed")) return "border-rose-200 bg-rose-50 text-rose-800";
  if (k === "llm") return "border-violet-200 bg-violet-50 text-violet-800";
  if (k === "tool") return "border-sky-200 bg-sky-50 text-sky-800";
  if (k === "policy") return "border-amber-200 bg-amber-50 text-amber-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function prettyJson(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function AgentStep({ step, index }: { step: Record<string, unknown>; index: number }) {
  const kind = textValue(step.kind) || "event";
  const action = textValue(step.action) || "step";
  const at = textValue(step.at);
  const toolName = textValue(step.tool_name) || textValue(step.tool);
  const model = textValue(step.model);
  const errorType = textValue(step.error_type);
  const duration = formatDurationMs(step.duration_ms);
  const ok = typeof step.ok === "boolean" ? step.ok : null;
  const body = Object.fromEntries(
    Object.entries(step).filter(([key]) => !["kind", "action", "at", "tool", "tool_name", "model", "tokens", "duration_ms", "ok", "error_type"].includes(key)),
  );
  const hasBody = Object.keys(body).length > 0;
  const defaultOpen = index < 2 || action.includes("failed") || Boolean(errorType);
  return (
    <div className="min-w-0 overflow-hidden rounded-md border border-slate-200 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="font-mono text-xs text-slate-500">#{index + 1}</span>
          <span className={`rounded border px-2 py-0.5 text-xs ${stepToneClass(kind, action)}`}>{kind}</span>
          <span className="truncate text-sm font-medium text-slate-900">{action}</span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-slate-500">
          {toolName ? <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono">{toolName}</span> : null}
          {model ? <span className="max-w-40 truncate rounded bg-slate-100 px-1.5 py-0.5 font-mono">{model}</span> : null}
          {duration ? <span>{duration}</span> : null}
          {ok !== null ? <span className={ok ? "text-emerald-700" : "text-rose-700"}>{ok ? "ok" : "error"}</span> : null}
          {errorType ? <span className="text-rose-700">{errorType}</span> : null}
          {at ? <span>{new Date(at).toLocaleTimeString()}</span> : null}
        </div>
      </div>
      {hasBody ? (
        <details className="mt-2 min-w-0" open={defaultOpen}>
          <summary className="cursor-pointer select-none text-xs font-medium text-slate-600">JSON</summary>
          <pre className="mt-2 max-h-72 max-w-full overflow-auto whitespace-pre rounded bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">{prettyJson(body)}</pre>
        </details>
      ) : null}
    </div>
  );
}

function AgentRunDetail({
  run,
  parentRun,
  childrenRuns = [],
  onSelectRun,
  onBackToParent,
  onClose,
}: {
  run: AgentRun;
  parentRun?: AgentRun | null;
  childrenRuns?: AgentRun[];
  onSelectRun: (run: AgentRun) => void;
  onBackToParent?: () => void;
  onClose: () => void;
}) {
  const translation = textValue(run.result?.translation);
  const confidence = numberValue(run.result?.confidence);
  const knowledgeStatus = textValue(run.result?.knowledge_status);
  const failureCategory = textValue(run.result?.failure_category);
  const displayStatus = agentDisplayStatus(run, childrenRuns);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-hidden overscroll-contain bg-slate-950/45 p-4">
      <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-[min(72rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-md bg-white shadow-xl">
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-200 p-4">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <div className="min-w-0 truncate text-lg font-semibold text-slate-950">{run.term || run.query || run.id}</div>
              <span title={displayStatus.title} className={`shrink-0 rounded border px-2 py-0.5 text-xs ${agentStatusClass(displayStatus.tone)}`}>
                {displayStatus.label}
              </span>
            </div>
            <div className="mt-1 truncate font-mono text-xs text-slate-500">{run.id}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {parentRun && onBackToParent ? (
              <button className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50" onClick={onBackToParent}>
                返回主 Agent
              </button>
            ) : null}
            <button className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>
        <div className="min-h-0 min-w-0 flex-1 gap-4 overflow-auto p-4 lg:grid lg:grid-cols-[300px_minmax(0,1fr)] lg:overflow-hidden">
          <div className="min-h-0 min-w-0 space-y-3 pr-1 lg:overflow-auto">
            <div className="min-w-0 rounded-md border border-slate-200 p-3">
              <div className="mb-2 text-xs font-semibold text-slate-600">概览</div>
              <div className="grid gap-1 text-xs text-slate-600">
                <div>status: <span title={displayStatus.title} className={`rounded border px-1.5 py-0.5 ${agentStatusClass(displayStatus.tone)}`}>{displayStatus.label}</span></div>
                <div>raw: <span className={`rounded border px-1.5 py-0.5 ${agentStatusClass(run.status)}`}>{run.status}</span></div>
                <div className="truncate">type: {run.agent_type}</div>
                {parentRun ? <div className="truncate">parent: {parentRun.term || parentRun.query || parentRun.id}</div> : null}
                <div className="truncate">domain: {run.domain || "-"}</div>
                <div className="truncate">target: {run.target_lang}</div>
                <div className="truncate">query: {run.query || "-"}</div>
                <div>started: {formatDate(run.started_at)}</div>
                <div>updated: {formatDate(run.updated_at)}</div>
                {run.finished_at ? <div>finished: {formatDate(run.finished_at)}</div> : null}
              </div>
            </div>
            <div className="min-w-0 rounded-md border border-slate-200 p-3">
              <div className="mb-2 text-xs font-semibold text-slate-600">结果</div>
              {translation ? (
                <div className="grid gap-1 text-xs text-slate-600">
                  <div className="text-sm font-medium text-slate-900">{translation}</div>
                  {confidence !== null ? <div>confidence: {(confidence * 100).toFixed(0)}%</div> : null}
                  {knowledgeStatus ? <div>knowledge: {knowledgeStatus}</div> : null}
                  {failureCategory ? <div>failure: {failureCategory}</div> : null}
                  {run.knowledge_item_id ? (
                    <Link to={knowledgeItemHref(run.knowledge_item_id)} className="truncate font-mono text-sky-700 hover:underline">
                      item: {run.knowledge_item_id}
                    </Link>
                  ) : null}
                </div>
              ) : (
                <div className="text-sm text-slate-500">暂无结果。</div>
              )}
              {run.error ? <div className="mt-2 max-h-32 overflow-auto rounded bg-rose-50 p-2 text-xs text-rose-700">{run.error}</div> : null}
            </div>
            {Object.keys(run.result || {}).length ? (
              <div className="min-w-0 rounded-md border border-slate-200 p-3">
                <div className="mb-2 text-xs font-semibold text-slate-600">Result JSON</div>
                <pre className="max-h-64 max-w-full overflow-auto whitespace-pre rounded bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">{prettyJson(run.result)}</pre>
              </div>
            ) : null}
            {childrenRuns.length ? (
              <div className="min-w-0 rounded-md border border-slate-200 p-3">
                <div className="mb-2 text-xs font-semibold text-slate-600">子 Agent</div>
                <div className="max-h-64 space-y-1 overflow-auto pr-1">
                  {childrenRuns.map((child) => (
                    <button
                      key={child.id}
                      type="button"
                      className="block w-full rounded border border-slate-200 px-2 py-1.5 text-left hover:bg-slate-50"
                      onClick={() => onSelectRun(child)}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 truncate text-xs font-medium text-slate-800">{child.term || child.query || child.id}</span>
                        {(() => {
                          const childDisplayStatus = agentDisplayStatus(child);
                          return (
                            <span title={childDisplayStatus.title} className={`shrink-0 rounded border px-1.5 py-0.5 text-[11px] ${agentStatusClass(childDisplayStatus.tone)}`}>
                              {childDisplayStatus.label}
                            </span>
                          );
                        })()}
                      </div>
                      <div className="mt-1 truncate font-mono text-[11px] text-slate-500">{child.agent_type} · {formatDate(child.updated_at)}</div>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
          <div className="mt-4 min-h-0 min-w-0 pr-1 lg:mt-0 lg:overflow-auto">
            <div className="mb-2 text-sm font-semibold text-slate-900">对话流</div>
            <div className="min-w-0 space-y-2">
              {run.steps.length === 0 ? <div className="text-sm text-slate-500">暂无步骤记录。</div> : null}
              {run.steps.map((step, index) => <AgentStep key={`${run.id}-${index}`} step={step} index={index} />)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function AgentRunCard({ run, onSelect, childrenRuns = [] }: { run: AgentRun; onSelect: (run: AgentRun) => void; childrenRuns?: AgentRun[] }) {
  const translation = textValue(run.result?.translation);
  const confidence = numberValue(run.result?.confidence);
  const knowledgeStatus = textValue(run.result?.knowledge_status);
  const failureCategory = textValue(run.result?.failure_category);
  const opened = run.steps.filter((step) => step.action === "open_url" || step.action === "read_url").length;
  const latestStep = run.steps[run.steps.length - 1];
  const displayStatus = agentDisplayStatus(run, childrenRuns);
  const failedChildren = childrenRuns.filter((child) => agentDisplayStatus(child).tone === "failed").length;
  const warningChildren = childrenRuns.filter((child) => agentDisplayStatus(child).tone === "warning").length;
  const runningChildren = childrenRuns.filter((child) => agentDisplayStatus(child).tone === "running").length;
  return (
    <button type="button" className="block w-full rounded-md border border-slate-200 p-3 text-left hover:bg-slate-50" onClick={() => onSelect(run)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-slate-950">{run.term || run.query || run.id}</div>
          <div className="mt-1 truncate font-mono text-xs text-slate-500">{run.agent_type} · {formatDate(run.updated_at)}</div>
        </div>
        <span title={displayStatus.title} className={`shrink-0 rounded border px-2 py-0.5 text-xs ${agentStatusClass(displayStatus.tone)}`}>{displayStatus.label}</span>
      </div>
      <div className="mt-2 grid gap-1 text-xs text-slate-600">
        {run.domain ? <div className="truncate">domain: {run.domain}</div> : null}
        {run.query ? <div className="truncate">query: {run.query}</div> : null}
        {opened ? <div>opened/read pages: {opened}</div> : null}
        {childrenRuns.length ? (
          <div>
            subagents: {childrenRuns.length}
            {runningChildren ? ` · running ${runningChildren}` : ""}
            {failedChildren ? ` · failed ${failedChildren}` : ""}
            {warningChildren ? ` · warning ${warningChildren}` : ""}
          </div>
        ) : null}
        {latestStep?.action ? <div className="truncate">latest: {String(latestStep.action)}</div> : null}
        {translation ? (
          <div className="truncate">
            result: {translation}
            {confidence !== null ? ` · ${(confidence * 100).toFixed(0)}%` : ""}
            {knowledgeStatus ? ` · ${knowledgeStatus}` : ""}
          </div>
        ) : null}
        {failureCategory ? <div className="truncate text-amber-700">failure: {failureCategory}</div> : null}
        {run.error ? <div className="line-clamp-2 text-rose-700">{run.error}</div> : null}
      </div>
      {childrenRuns.length ? (
        <div className="mt-3 space-y-1 border-l border-slate-200 pl-3">
          {childrenRuns.slice(0, 5).map((child) => (
            <div key={child.id} className="flex items-center justify-between gap-2 text-xs text-slate-600">
              <span className="min-w-0 truncate">{child.term || child.query || child.id}</span>
              {(() => {
                const childDisplayStatus = agentDisplayStatus(child);
                return (
                  <span title={childDisplayStatus.title} className={`shrink-0 rounded border px-1.5 py-0.5 ${agentStatusClass(childDisplayStatus.tone)}`}>
                    {childDisplayStatus.label}
                  </span>
                );
              })()}
            </div>
          ))}
          {childrenRuns.length > 5 ? <div className="text-xs text-slate-500">还有 {childrenRuns.length - 5} 个子 Agent，点开查看。</div> : null}
        </div>
      ) : null}
    </button>
  );
}

function ResourceBar({ label, value, detail, tone = "sky" }: { label: string; value?: number | null; detail?: string; tone?: "sky" | "emerald" | "amber" }) {
  const percent = clampPercent(value);
  const color =
    tone === "emerald"
      ? "bg-emerald-500"
      : tone === "amber"
        ? "bg-amber-500"
        : "bg-sky-500";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-3 text-xs">
        <span className="font-medium text-slate-700">{label}</span>
        <span className="font-mono text-slate-600">{formatPercent(value)}</span>
      </div>
      <div className="h-3 overflow-hidden rounded-sm bg-slate-200 dark:bg-slate-800">
        <div className={`h-full ${color}`} style={{ width: `${percent}%` }} />
      </div>
      {detail ? <div className="mt-1 truncate text-xs text-slate-500">{detail}</div> : null}
    </div>
  );
}

const runningStatuses = new Set(["INGESTED", "DOWNLOADED", "AUDIO_EXTRACTED", "ASR_DONE", "TRANSLATED", "SUBTITLE_READY", "RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING"]);

export default function DashboardPage() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [videos, setVideos] = useState<ConvertedVideoItem[] | null>(null);
  const [videosError, setVideosError] = useState<string | null>(null);
  const [resources, setResources] = useState<ResourceSnapshot | null>(null);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [agentRuns, setAgentRuns] = useState<AgentRun[] | null>(null);
  const [agentRunsError, setAgentRunsError] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?limit=200`)
      .then((data) => setTasks(data))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=12`)
      .then((data) => setVideos(data))
      .catch((e: unknown) => setVideosError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const data = await fetchJson<ResourceSnapshot>(`${ORCHESTRATOR_URL}/system/resources`);
        if (cancelled) return;
        setResources(data);
        setResourcesError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setResourcesError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) timer = window.setTimeout(load, 3000);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const data = await fetchJson<AgentRun[]>(`${ORCHESTRATOR_URL}/subtitle/agents/runs?limit=80`);
        if (cancelled) return;
        setAgentRuns(data);
        setAgentRunsError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setAgentRunsError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) timer = window.setTimeout(load, 5000);
      }
    };
    load();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const counts = useMemo(() => {
    const out = new Map<string, number>();
    for (const t of tasks ?? []) out.set(t.status, (out.get(t.status) ?? 0) + 1);
    return Array.from(out.entries()).sort((a, b) => b[1] - a[1]);
  }, [tasks]);

  const total = tasks?.length ?? 0;
  const failedTasks = useMemo(() => (tasks ?? []).filter((t) => t.status === "FAILED").slice(0, 6), [tasks]);
  const runningTasks = useMemo(() => (tasks ?? []).filter((t) => runningStatuses.has(t.status)).slice(0, 6), [tasks]);
  const agentChildren = useMemo(() => {
    const out = new Map<string, AgentRun[]>();
    for (const run of agentRuns ?? []) {
      if (!run.parent_agent_run_id) continue;
      const rows = out.get(run.parent_agent_run_id) ?? [];
      rows.push(run);
      out.set(run.parent_agent_run_id, rows);
    }
    return out;
  }, [agentRuns]);
  const agentById = useMemo(() => {
    const out = new Map<string, AgentRun>();
    for (const run of agentRuns ?? []) out.set(run.id, run);
    return out;
  }, [agentRuns]);
  const topLevelAgents = useMemo(() => (agentRuns ?? []).filter((run) => !run.parent_agent_run_id), [agentRuns]);
  const runningAgents = useMemo(
    () => topLevelAgents.filter((run) => agentDisplayStatus(run, agentChildren.get(run.id) ?? []).tone === "running"),
    [agentChildren, topLevelAgents],
  );
  const recentFinishedAgents = useMemo(
    () => topLevelAgents.filter((run) => agentDisplayStatus(run, agentChildren.get(run.id) ?? []).tone !== "running").slice(0, 6),
    [agentChildren, topLevelAgents],
  );
  const selectedAgent = useMemo(() => (agentRuns ?? []).find((run) => run.id === selectedAgentId) ?? null, [agentRuns, selectedAgentId]);
  const selectedAgentParent = useMemo(
    () => (selectedAgent?.parent_agent_run_id ? agentById.get(selectedAgent.parent_agent_run_id) ?? null : null),
    [agentById, selectedAgent],
  );
  useEffect(() => {
    if (!selectedAgentId || selectedAgent || !agentRuns) return;
    setSelectedAgentId(null);
  }, [agentRuns, selectedAgent, selectedAgentId]);
  useEffect(() => {
    if (!selectedAgent) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [selectedAgent]);
  const publishedCount = counts.find(([status]) => status === "PUBLISHED")?.[1] ?? 0;
  const failedCount = counts.find(([status]) => status === "FAILED")?.[1] ?? 0;
  const runningCount = (tasks ?? []).filter((t) => runningStatuses.has(t.status)).length;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dashboard"
        description="快速查看任务状态、失败任务和最新产物。"
        actions={
          <>
            <Link to="/tasks?status=FAILED" className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50">
              查看失败
            </Link>
            <Link to="/tasks/new" className="rounded-md bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800">
            新建任务
            </Link>
          </>
        }
      />

      <div className="vr-section">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">最近任务（200 条内）</div>
          {tasks ? <div className="text-xs text-slate-500">共 {total} 条</div> : null}
        </div>
        {error ? <div className="text-sm text-rose-700">{error}</div> : null}
        {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {tasks ? (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Link to="/tasks" className="rounded-md border border-slate-200 bg-slate-50 p-3 hover:bg-slate-100">
                <div className="text-xs text-slate-500">全部任务</div>
                <div className="mt-1 text-2xl font-semibold text-slate-950">{total}</div>
              </Link>
              <Link to="/queue/render" className="rounded-md border border-sky-200 bg-sky-50 p-3 hover:bg-sky-100">
                <div className="text-xs text-sky-700">运行中</div>
                <div className="mt-1 text-2xl font-semibold text-sky-950">{runningCount}</div>
              </Link>
              <Link to="/tasks?status=FAILED" className="rounded-md border border-rose-200 bg-rose-50 p-3 hover:bg-rose-100">
                <div className="text-xs text-rose-700">失败</div>
                <div className="mt-1 text-2xl font-semibold text-rose-950">{failedCount}</div>
              </Link>
              <Link to="/tasks?status=PUBLISHED" className="rounded-md border border-emerald-200 bg-emerald-50 p-3 hover:bg-emerald-100">
                <div className="text-xs text-emerald-700">已发布</div>
                <div className="mt-1 text-2xl font-semibold text-emerald-950">{publishedCount}</div>
              </Link>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
            {counts.map(([status, n]) => (
                <Link key={status} to={`/tasks?status=${status}`} className="rounded-md border border-slate-200 p-3 hover:bg-slate-50">
                <div className="text-xs text-slate-500">{status}</div>
                  <div className="text-xl font-semibold text-slate-900">{n}</div>
                </Link>
            ))}
            </div>
          </>
        ) : null}
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">资源监控</div>
            <div className="mt-1 text-xs text-slate-500">每 3 秒刷新一次，数值来自当前运行后端的容器/主机视角。</div>
          </div>
          {resources?.sampled_at ? <div className="text-xs text-slate-500">{new Date(resources.sampled_at).toLocaleTimeString()}</div> : null}
        </div>
        {resourcesError ? <div className="mt-3 text-sm text-rose-700">{resourcesError}</div> : null}
        {!resources ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
        {resources ? (
          <div className="mt-4 grid gap-4 lg:grid-cols-3">
            <div className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
              <ResourceBar
                label={`CPU${resources.cpu.cores ? ` · ${resources.cpu.cores} cores` : ""}`}
                value={resources.cpu.percent}
                detail={resources.cpu.load_average?.length ? `load ${resources.cpu.load_average.map((n) => n.toFixed(2)).join(" / ")}` : undefined}
              />
            </div>
            <div className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
              <ResourceBar
                label={resources.cgroup_memory ? "Memory · cgroup" : "Memory"}
                value={(resources.cgroup_memory ?? resources.memory).percent}
                detail={`${formatBytes((resources.cgroup_memory ?? resources.memory).used_bytes)} / ${formatBytes((resources.cgroup_memory ?? resources.memory).total_bytes)}`}
                tone="emerald"
              />
              {resources.cgroup_memory ? (
                <div className="mt-2 text-xs text-slate-500">host: {formatBytes(resources.memory.used_bytes)} / {formatBytes(resources.memory.total_bytes)}</div>
              ) : null}
            </div>
            {resources.intel_gpu?.enabled ? (
              <div className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
                <ResourceBar
                  label="Intel GPU"
                  value={resources.intel_gpu.usage_percent}
                  detail={
                    resources.intel_gpu.available
                      ? `${resources.intel_gpu.model_name || resources.intel_gpu.render_device}${resources.intel_gpu.usage_supported ? "" : " · busy 不可读"}`
                      : resources.intel_gpu.detail || "未检测到可用 Intel GPU"
                  }
                  tone="amber"
                />
                {resources.intel_gpu.engines?.length ? (
                  <div className="mt-2 grid gap-1">
                    {resources.intel_gpu.engines.slice(0, 4).map((engine) => (
                      <div key={engine.name} className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
                        <span className="truncate">{engine.name}</span>
                        <span className="font-mono">{formatPercent(engine.percent)}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">RAG Agent</div>
            <div className="mt-1 text-xs text-slate-500">显示术语发现、搜索、网页读取、总结入库的运行记录。</div>
          </div>
          {agentRuns ? <div className="text-xs text-slate-500">运行中 {runningAgents.length}</div> : null}
        </div>
        {agentRunsError ? <div className="mt-3 text-sm text-rose-700">{agentRunsError}</div> : null}
        {!agentRuns ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
        {agentRuns ? (
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div>
              <div className="mb-2 text-xs font-semibold text-slate-600">正在工作</div>
              <div className="space-y-2">
                {runningAgents.length === 0 ? <div className="text-sm text-slate-500">暂无运行中的 agent。</div> : null}
                {runningAgents.map((run) => <AgentRunCard key={run.id} run={run} childrenRuns={agentChildren.get(run.id) ?? []} onSelect={(item) => setSelectedAgentId(item.id)} />)}
              </div>
            </div>
            <div>
              <div className="mb-2 text-xs font-semibold text-slate-600">最近结果</div>
              <div className="space-y-2">
                {recentFinishedAgents.length === 0 ? <div className="text-sm text-slate-500">暂无 agent 结果。</div> : null}
                {recentFinishedAgents.map((run) => <AgentRunCard key={run.id} run={run} childrenRuns={agentChildren.get(run.id) ?? []} onSelect={(item) => setSelectedAgentId(item.id)} />)}
              </div>
            </div>
          </div>
        ) : null}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="vr-section">
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm font-semibold">正在处理</div>
            <Link to="/queue/render" className="text-sm text-slate-700 hover:underline">
              队列 →
            </Link>
          </div>
          <div className="mt-3 space-y-2">
            {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
            {tasks && runningTasks.length === 0 ? <div className="text-sm text-slate-500">暂无运行中任务。</div> : null}
            {runningTasks.map((t) => (
              <Link key={t.id} to={`/tasks/${t.id}`} className="block rounded-md border border-slate-200 p-3 hover:bg-slate-50">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-950">{t.display_title?.trim() || t.source_url || t.id}</div>
                    <div className="mt-1 font-mono text-xs text-slate-500">{t.id.slice(0, 8)} · {formatDate(t.updated_at)}</div>
                  </div>
                  <StatusBadge status={t.status} />
                </div>
              </Link>
            ))}
          </div>
        </div>

        <div className="vr-section">
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm font-semibold">最近失败</div>
            <Link to="/tasks?status=FAILED" className="text-sm text-slate-700 hover:underline">
              全部失败 →
            </Link>
          </div>
          <div className="mt-3 space-y-2">
            {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
            {tasks && failedTasks.length === 0 ? <div className="text-sm text-slate-500">暂无失败任务。</div> : null}
            {failedTasks.map((t) => (
              <Link key={t.id} to={`/tasks/${t.id}`} className="block rounded-md border border-rose-100 p-3 hover:bg-rose-50">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-950">{t.display_title?.trim() || t.source_url || t.id}</div>
                    <div className="mt-1 line-clamp-2 text-xs text-rose-700">{t.error_message || t.error_code || "FAILED"}</div>
                  </div>
                  <StatusBadge status={t.status} />
                </div>
              </Link>
            ))}
          </div>
        </div>
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">已转换视频（video_final）</div>
          <Link to="/videos" className="text-sm text-slate-700 hover:underline">
            管理全部 →
          </Link>
        </div>
        <div className="mt-1 text-xs text-slate-500">展示最近 12 条已生成最终视频的任务，可下载/进入详情继续操作。</div>
        {videosError ? <div className="mt-2 text-sm text-rose-700">{videosError}</div> : null}
        {!videos ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {videos ? (
          <div className="mt-3 overflow-auto rounded-md border border-slate-200">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Video</th>
                  <th className="py-2 pr-3">Task</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {videos.map((it) => (
                  <tr key={it.final_asset.id} className="border-t">
                    <td className="py-2 pr-3">
                      <div className="font-mono text-xs">{fileNameFromKey(it.final_asset.storage_key)}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <Link to={`/tasks/${it.task.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                        {it.task.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td className="py-2 pr-3">
                      <StatusBadge status={it.task.status} />
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}/download`}
                        >
                          Download
                        </a>
                        <Link className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50" to={`/tasks/${it.task.id}`}>
                          Detail
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
                {videos.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-6 text-center text-sm text-slate-500">
                      暂无
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
      {selectedAgent ? (
        <AgentRunDetail
          run={selectedAgent}
          parentRun={selectedAgentParent}
          childrenRuns={agentChildren.get(selectedAgent.id) ?? []}
          onSelectRun={(run) => setSelectedAgentId(run.id)}
          onBackToParent={selectedAgentParent ? () => setSelectedAgentId(selectedAgentParent.id) : undefined}
          onClose={() => setSelectedAgentId(null)}
        />
      ) : null}
    </div>
  );
}
