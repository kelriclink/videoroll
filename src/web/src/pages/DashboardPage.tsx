import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import StatusBadge from "../components/StatusBadge";
import { Asset, Task } from "../lib/types";
import { PageHeader } from "../components/ui";
import { RealtimeEvent, useRealtimeSubscription } from "../lib/realtime";

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

function agentTreeDisplayStatus(
  run: AgentRun,
  childrenByParent: Map<string, AgentRun[]>,
  visiting: Set<string> = new Set(),
): AgentDisplayStatus {
  if (visiting.has(run.id)) return agentDisplayStatus(run);
  const nextVisiting = new Set(visiting);
  nextVisiting.add(run.id);
  const children = childrenByParent.get(run.id) ?? [];
  const childStatuses = children.map((child) => agentTreeDisplayStatus(child, childrenByParent, nextVisiting));
  const runningChildren = childStatuses.filter((child) => child.tone === "running").length;
  const failedChildren = childStatuses.filter((child) => child.tone === "failed").length;
  const warningChildren = childStatuses.filter((child) => child.tone === "warning").length;
  const own = agentDisplayStatus(run);
  if (own.tone === "failed") return own;
  if (own.tone === "running" || runningChildren > 0) {
    return {
      label: own.tone === "running" ? "running" : "waiting",
      tone: "running",
      title: runningChildren > 0 ? `${runningChildren} 个分支仍在运行` : own.title,
    };
  }
  if (failedChildren > 0) return { label: "partial", tone: "warning", title: `${failedChildren} 个下级分支失败` };
  if (own.tone === "warning" || warningChildren > 0) {
    return { label: "partial", tone: "warning", title: own.tone === "warning" ? own.title : `${warningChildren} 个下级分支部分完成` };
  }
  return own;
}

function collectAgentTree(root: AgentRun, childrenByParent: Map<string, AgentRun[]>): Array<{ run: AgentRun; depth: number }> {
  const rows: Array<{ run: AgentRun; depth: number }> = [];
  const visited = new Set<string>();
  const visit = (run: AgentRun, depth: number) => {
    if (visited.has(run.id)) return;
    visited.add(run.id);
    rows.push({ run, depth });
    for (const child of childrenByParent.get(run.id) ?? []) visit(child, depth + 1);
  };
  visit(root, 0);
  return rows;
}

function latestAgentInTree(root: AgentRun, childrenByParent: Map<string, AgentRun[]>): AgentRun {
  const rows = collectAgentTree(root, childrenByParent);
  const descendants = rows.filter((row) => row.depth > 0);
  const candidates = descendants.length ? descendants : rows;
  return [...candidates].sort((a, b) => {
    const aRunning = (a.run.status || "").toLowerCase() === "running" ? 1 : 0;
    const bRunning = (b.run.status || "").toLowerCase() === "running" ? 1 : 0;
    if (aRunning !== bRunning) return bRunning - aRunning;
    const timeDiff = new Date(b.run.updated_at).getTime() - new Date(a.run.updated_at).getTime();
    if (timeDiff !== 0) return timeDiff;
    return b.depth - a.depth;
  })[0]?.run ?? root;
}

function agentTypeLabel(run: AgentRun): string {
  if (run.agent_type === "subtitle_translation_session") return "翻译 Session";
  if (run.agent_type === "subtitle_translation_batch") return "翻译 Batch";
  if (run.agent_type === "rag_master") return "RAG Master";
  if (run.agent_type === "rag_term_research") return "RAG 子 Agent";
  if (run.agent_type === "subtitle_translation_thinking") return "翻译 Think";
  return run.agent_type;
}

function translationSessionStats(run: AgentRun, childrenByParent: Map<string, AgentRun[]>) {
  const batches = (childrenByParent.get(run.id) ?? []).filter((child) => child.agent_type === "subtitle_translation_batch");
  const completedFromBatches = batches.reduce((max, batch) => Math.max(max, numberValue(batch.result?.completed_segments) ?? 0), 0);
  const total = numberValue(run.result?.total_segments)
    ?? numberValue(recordValue(run.steps[0]?.input).segment_count)
    ?? 0;
  const completed = numberValue(run.result?.completed_segments) ?? completedFromBatches;
  const failed = batches.filter((batch) => agentTreeDisplayStatus(batch, childrenByParent).tone === "failed").length;
  const running = batches.filter((batch) => agentTreeDisplayStatus(batch, childrenByParent).tone === "running").length;
  const succeeded = batches.filter((batch) => agentTreeDisplayStatus(batch, childrenByParent).tone === "success").length;
  return { batches, total, completed, failed, running, succeeded };
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

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function thinkingDelta(step: Record<string, unknown>): string {
  return textValue(recordValue(step.output).thinking_delta);
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
  const thinking = thinkingDelta(step);
  const batchStart = numberValue(recordValue(step.input).batch_start);
  const batchSize = numberValue(recordValue(step.input).batch_size);
  const output = recordValue(step.output);
  const finalTranslations = action === "translation_batch.completed" && Array.isArray(output.translations)
    ? output.translations.map(recordValue).filter((item) => textValue(item.text))
    : [];
  const body = Object.fromEntries(
    Object.entries(step).filter(([key]) => !["kind", "action", "at", "tool", "tool_name", "model", "tokens", "duration_ms", "ok", "error_type"].includes(key) && !(thinking && key === "output")),
  );
  const hasBody = Object.keys(body).length > 0;
  const defaultOpen = index < 2 || action.includes("failed") || Boolean(errorType) || Boolean(thinking);
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
      {thinking ? (
        <div className="mt-2 rounded border border-violet-200 bg-violet-50 p-3">
          <div className="mb-1 text-xs font-medium text-violet-900">
            Think 流{batchStart !== null ? ` · 字幕 ${batchStart}${batchSize && batchSize > 1 ? `-${batchStart + batchSize - 1}` : ""}` : ""}
          </div>
          <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-violet-950">{thinking}</pre>
        </div>
      ) : null}
      {finalTranslations.length ? (
        <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50/70 p-3">
          <div className="mb-2 text-xs font-semibold text-emerald-900">最终翻译 · {finalTranslations.length} 段</div>
          <div className="max-h-96 space-y-2 overflow-auto pr-1">
            {finalTranslations.map((item, itemIndex) => (
              <div key={`${String(item.idx || itemIndex)}-${itemIndex}`} className="grid grid-cols-[auto_minmax(0,1fr)] gap-2 text-sm leading-relaxed text-slate-900">
                <span className="font-mono text-xs text-emerald-700">#{String(item.idx || itemIndex + 1)}</span>
                <span className="whitespace-pre-wrap">{textValue(item.text)}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {hasBody ? (
        <details className="mt-2 min-w-0" open={defaultOpen}>
          <summary className="cursor-pointer select-none text-xs font-medium text-slate-600">JSON</summary>
          <pre className="mt-2 max-h-72 max-w-full overflow-auto whitespace-pre rounded bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">{prettyJson(body)}</pre>
        </details>
      ) : null}
    </div>
  );
}

type AgentStepGroup =
  | { type: "reasoning"; steps: Array<Record<string, unknown>> }
  | { type: "tools"; steps: Array<Record<string, unknown>> }
  | { type: "step"; step: Record<string, unknown> };

function groupedAgentSteps(steps: Array<Record<string, unknown>>): AgentStepGroup[] {
  const groups: AgentStepGroup[] = [];
  for (const step of steps) {
    const action = textValue(step.action);
    const kind = textValue(step.kind);
    const type = action === "translation_thinking.delta" ? "reasoning" : kind === "tool" ? "tools" : "step";
    const last = groups[groups.length - 1];
    if (type === "reasoning" && last?.type === "reasoning") last.steps.push(step);
    else if (type === "tools" && last?.type === "tools") last.steps.push(step);
    else if (type === "reasoning" || type === "tools") groups.push({ type, steps: [step] });
    else groups.push({ type: "step", step });
  }
  return groups;
}

function AgentFlowDisclosure({
  title,
  tone,
  active,
  children,
}: {
  title: string;
  tone: "reasoning" | "tools";
  active: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(active);
  const [manuallyToggled, setManuallyToggled] = useState(false);
  useEffect(() => {
    if (!manuallyToggled) setOpen(active);
  }, [active, manuallyToggled]);
  const classes = tone === "reasoning"
    ? "border-violet-200 bg-violet-50/70 text-violet-800"
    : "border-sky-200 bg-sky-50/60 text-sky-800";
  return (
    <div className={`rounded-lg border p-3 ${classes}`}>
      <button type="button" className="flex w-full items-center justify-between gap-3 text-left text-xs font-semibold" onClick={() => {
        setManuallyToggled(true);
        setOpen((value) => !value);
      }}>
        <span>{title}</span>
        <span className="shrink-0 text-[11px] opacity-70">{open ? "收起" : "展开"}</span>
      </button>
      {open ? <div className="mt-3">{children}</div> : null}
    </div>
  );
}

function AgentExecutionFlow({ run, followLatest, endRef }: { run: AgentRun; followLatest: boolean; endRef: { current: HTMLDivElement | null } }) {
  const groups = groupedAgentSteps(run.steps);
  const running = (run.status || "").toLowerCase() === "running";
  const groupStartIndices: number[] = [];
  let nextStepIndex = 0;
  for (const group of groups) {
    groupStartIndices.push(nextStepIndex);
    nextStepIndex += group.type === "step" ? 1 : group.steps.length;
  }
  return (
    <div className="min-w-0 space-y-2">
      {groups.length === 0 ? <div className="rounded-lg border border-dashed border-slate-200 p-8 text-center text-sm text-slate-500">等待此节点产生执行记录…</div> : null}
      {groups.map((group, groupIndex) => {
        if (group.type === "reasoning") {
          const text = group.steps.map(thinkingDelta).filter(Boolean).join("");
          return (
            <AgentFlowDisclosure key={`reasoning-${groupIndex}`} tone="reasoning" active={running && followLatest} title={`${running ? "正在思考…" : "思考过程"} · ${text.length} 字符`}>
              <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-violet-950">{text}</pre>
            </AgentFlowDisclosure>
          );
        }
        if (group.type === "tools") {
          const names = group.steps.map((step) => textValue(step.tool_name) || textValue(step.action) || "tool");
          const startIndex = groupStartIndices[groupIndex];
          return (
            <AgentFlowDisclosure key={`tools-${groupIndex}`} tone="tools" active={running && followLatest} title={`${running ? "正在调用工具" : "已调用工具"} · ${names.length} 次 · ${Array.from(new Set(names)).slice(0, 3).join("、")}`}>
              <div className="mt-3 space-y-2">{group.steps.map((step, index) => <AgentStep key={`${run.id}-tool-${groupIndex}-${index}`} step={step} index={startIndex + index} />)}</div>
            </AgentFlowDisclosure>
          );
        }
        return <AgentStep key={`${run.id}-step-${groupIndex}`} step={group.step} index={groupStartIndices[groupIndex]} />;
      })}
      <div ref={endRef} />
    </div>
  );
}

function AgentRunDetail({
  rootRun,
  activeRun,
  childrenByParent,
  followLatest,
  onSelectRun,
  onToggleFollowLatest,
  onClose,
}: {
  rootRun: AgentRun;
  activeRun: AgentRun;
  childrenByParent: Map<string, AgentRun[]>;
  followLatest: boolean;
  onSelectRun: (run: AgentRun) => void;
  onToggleFollowLatest: () => void;
  onClose: () => void;
}) {
  const displayStatus = agentTreeDisplayStatus(rootRun, childrenByParent);
  const activeStatus = agentTreeDisplayStatus(activeRun, childrenByParent);
  const treeRows = collectAgentTree(rootRun, childrenByParent);
  const flowEndRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (followLatest) flowEndRef.current?.scrollIntoView({ block: "end" });
  }, [activeRun.id, activeRun.steps.length, followLatest]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-hidden overscroll-contain bg-slate-950/45 p-4">
      <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-[min(86rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-xl bg-white shadow-2xl">
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-200 p-4">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <div className="min-w-0 truncate text-lg font-semibold text-slate-950">{rootRun.term || rootRun.query || rootRun.id}</div>
              <span title={displayStatus.title} className={`shrink-0 rounded border px-2 py-0.5 text-xs ${agentStatusClass(displayStatus.tone)}`}>{displayStatus.label}</span>
            </div>
            <div className="mt-1 truncate text-xs text-slate-500">当前：{activeRun.term || activeRun.query || activeRun.id} · {agentTypeLabel(activeRun)}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button className={`rounded-md border px-3 py-2 text-sm ${followLatest ? "border-violet-200 bg-violet-50 text-violet-800" : "border-slate-300 hover:bg-slate-50"}`} onClick={onToggleFollowLatest}>
              {followLatest ? "正在跟随最新" : "跟随最新"}
            </button>
            <button className="rounded-md border border-slate-300 px-3 py-2 text-sm hover:bg-slate-50" onClick={onClose}>关闭</button>
          </div>
        </div>
        <div className="min-h-0 min-w-0 flex-1 overflow-auto lg:grid lg:grid-cols-[360px_minmax(0,1fr)] lg:overflow-hidden">
          <div className="min-h-0 min-w-0 border-r border-slate-200 bg-slate-50/70 p-4 lg:overflow-auto">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">执行树</div>
              <div className="text-[11px] text-slate-400">{treeRows.length} 个节点</div>
            </div>
            <div className="mb-4 space-y-1">
              {treeRows.map(({ run, depth }) => {
                const status = agentTreeDisplayStatus(run, childrenByParent);
                const selected = run.id === activeRun.id;
                return (
                  <button key={run.id} type="button" className={`block w-full rounded-lg border px-2.5 py-2 text-left transition ${selected ? "border-violet-300 bg-white shadow-sm ring-1 ring-violet-100" : "border-transparent hover:border-slate-200 hover:bg-white"}`} style={{ paddingLeft: `${10 + Math.min(depth, 5) * 16}px` }} onClick={() => onSelectRun(run)}>
                    <div className="flex items-center gap-2">
                      <span className={`h-2 w-2 shrink-0 rounded-full ${status.tone === "running" ? "animate-pulse bg-sky-500" : status.tone === "success" ? "bg-emerald-500" : status.tone === "failed" ? "bg-rose-500" : status.tone === "warning" ? "bg-amber-500" : "bg-slate-400"}`} />
                      <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-800">{run.term || run.query || run.id}</span>
                      <span className="shrink-0 text-[10px] text-slate-400">{agentTypeLabel(run)}</span>
                    </div>
                  </button>
                );
              })}
            </div>
            <div className="rounded-lg border border-slate-200 bg-white p-3">
              <div className="mb-2 flex items-center justify-between gap-2 text-xs font-semibold text-slate-600">
                <span>当前节点</span>
                <span title={activeStatus.title} className={`rounded border px-1.5 py-0.5 font-normal ${agentStatusClass(activeStatus.tone)}`}>{activeStatus.label}</span>
              </div>
              <div className="grid gap-1 text-xs text-slate-600">
                <div className="font-medium text-slate-900">{activeRun.term || activeRun.query || activeRun.id}</div>
                <div className="truncate">type: {agentTypeLabel(activeRun)}</div>
                <div className="truncate">query: {activeRun.query || "-"}</div>
                <div>updated: {formatDate(activeRun.updated_at)}</div>
              </div>
              {activeRun.error ? <div className="mt-3 max-h-32 overflow-auto rounded bg-rose-50 p-2 text-xs text-rose-700">{activeRun.error}</div> : null}
            </div>
            {Object.keys(activeRun.result || {}).length ? (
              <details className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                <summary className="cursor-pointer text-xs font-semibold text-slate-600">结果数据</summary>
                <pre className="mt-2 max-h-64 max-w-full overflow-auto whitespace-pre rounded bg-slate-950 p-3 text-xs leading-relaxed text-slate-100">{prettyJson(activeRun.result)}</pre>
              </details>
            ) : null}
          </div>
          <div className="min-h-0 min-w-0 bg-white p-4 lg:flex lg:flex-col lg:overflow-hidden">
            <div className="mb-3 flex shrink-0 items-start justify-between gap-3 border-b border-slate-100 pb-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-slate-900">最新执行流</div>
                <div className="mt-1 truncate text-xs text-slate-500">{activeRun.term || activeRun.query || activeRun.id}</div>
              </div>
              <span className="rounded-full bg-violet-50 px-2 py-1 text-[11px] text-violet-700">{activeRun.steps.length} steps</span>
            </div>
            <div className="min-w-0 lg:min-h-0 lg:flex-1 lg:overflow-auto lg:pr-1">
              <AgentExecutionFlow run={activeRun} followLatest={followLatest} endRef={flowEndRef} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function AgentRunCard({ run, onSelect, childrenByParent }: { run: AgentRun; onSelect: (run: AgentRun) => void; childrenByParent: Map<string, AgentRun[]> }) {
  const childrenRuns = childrenByParent.get(run.id) ?? [];
  const translation = textValue(run.result?.translation);
  const confidence = numberValue(run.result?.confidence);
  const knowledgeStatus = textValue(run.result?.knowledge_status);
  const failureCategory = textValue(run.result?.failure_category);
  const opened = run.steps.filter((step) => step.action === "open_url" || step.action === "read_url").length;
  const latestStep = run.steps[run.steps.length - 1];
  const latestThinking = [...run.steps].reverse().map(thinkingDelta).find(Boolean) || "";
  const displayStatus = agentTreeDisplayStatus(run, childrenByParent);
  const failedChildren = childrenRuns.filter((child) => agentTreeDisplayStatus(child, childrenByParent).tone === "failed").length;
  const warningChildren = childrenRuns.filter((child) => agentTreeDisplayStatus(child, childrenByParent).tone === "warning").length;
  const runningChildren = childrenRuns.filter((child) => agentTreeDisplayStatus(child, childrenByParent).tone === "running").length;
  const sessionStats = run.agent_type === "subtitle_translation_session" ? translationSessionStats(run, childrenByParent) : null;
  const latestNode = latestAgentInTree(run, childrenByParent);
  const progress = sessionStats?.total ? Math.max(0, Math.min(100, (sessionStats.completed / sessionStats.total) * 100)) : 0;
  return (
    <button type="button" className="block w-full rounded-md border border-slate-200 p-3 text-left hover:bg-slate-50" onClick={() => onSelect(run)}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-slate-950">{run.term || run.query || run.id}</div>
          <div className="mt-1 truncate text-xs text-slate-500">{agentTypeLabel(run)} · {formatDate(run.updated_at)}</div>
        </div>
        <span title={displayStatus.title} className={`shrink-0 rounded border px-2 py-0.5 text-xs ${agentStatusClass(displayStatus.tone)}`}>{displayStatus.label}</span>
      </div>
      <div className="mt-2 grid gap-1 text-xs text-slate-600">
        {sessionStats ? (
          <>
            <div className="flex items-center justify-between gap-2">
              <span>进度：{sessionStats.completed}/{sessionStats.total || "-"} 段</span>
              <span>{progress.toFixed(0)}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-violet-500 transition-all" style={{ width: `${progress}%` }} /></div>
            <div>Batch：{sessionStats.batches.length}{sessionStats.running ? ` · 运行 ${sessionStats.running}` : ""}{sessionStats.succeeded ? ` · 成功 ${sessionStats.succeeded}` : ""}{sessionStats.failed ? ` · 失败 ${sessionStats.failed}` : ""}</div>
            <div className="truncate text-violet-700">当前：{latestNode.term || latestNode.query || latestNode.id}</div>
          </>
        ) : null}
        {!sessionStats && run.domain ? <div className="truncate">domain: {run.domain}</div> : null}
        {!sessionStats && run.query ? <div className="truncate">query: {run.query}</div> : null}
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
        {latestThinking ? <div className="line-clamp-2 text-violet-800">think: {latestThinking}</div> : null}
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
                const childDisplayStatus = agentTreeDisplayStatus(child, childrenByParent);
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

function upsertTask(current: Task[] | null, task: Task, limit: number): Task[] {
  const rows = current ?? [];
  const index = rows.findIndex((item) => item.id === task.id);
  if (index < 0) return [task, ...rows].slice(0, limit);
  const next = [...rows];
  next[index] = { ...rows[index], ...task };
  return next;
}

function updateTaskUpload(current: Task[] | null, job: Record<string, unknown>): Task[] | null {
  if (!current) return current;
  const taskId = String(job.task_id ?? "");
  const jobId = String(job.id ?? "");
  return current.map((task) => {
    if (task.id !== taskId) return task;
    if (job.upload_active) {
      return {
        ...task,
        bilibili_upload: {
          job_id: jobId,
          progress: Math.floor(clampPercent(Number(job.upload_progress ?? 0))),
        },
      };
    }
    return task.bilibili_upload?.job_id === jobId ? { ...task, bilibili_upload: null } : task;
  });
}

export default function DashboardPage() {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [publishingTasks, setPublishingTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [videos, setVideos] = useState<ConvertedVideoItem[] | null>(null);
  const [videosError, setVideosError] = useState<string | null>(null);
  const [resources, setResources] = useState<ResourceSnapshot | null>(null);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [agentRuns, setAgentRuns] = useState<AgentRun[] | null>(null);
  const [agentRunsError, setAgentRunsError] = useState<string | null>(null);
  const [selectedAgentRootId, setSelectedAgentRootId] = useState<string | null>(null);
  const [selectedAgentNodeId, setSelectedAgentNodeId] = useState<string | null>(null);
  const [followLatestAgent, setFollowLatestAgent] = useState(true);
  const agentListTimerRef = useRef<number | undefined>();
  const selectedAgentTimerRef = useRef<number | undefined>();
  const uploadByTaskRef = useRef(new Map<string, Task["bilibili_upload"]>());

  const loadTasks = useCallback(async () => {
    try {
      const data = await fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?limit=200`);
      for (const task of data) uploadByTaskRef.current.set(task.id, task.bilibili_upload ?? null);
      setTasks(data);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadPublishingTasks = useCallback(async () => {
    try {
      setPublishingTasks(await fetchJson<Task[]>(`${ORCHESTRATOR_URL}/tasks?status=PUBLISHING&limit=6`));
    } catch {
      // Keep the last successful snapshot; the regular task list remains available.
    }
  }, []);

  const loadResources = useCallback(async () => {
    try {
      setResources(await fetchJson<ResourceSnapshot>(`${ORCHESTRATOR_URL}/system/resources`));
      setResourcesError(null);
    } catch (e: unknown) {
      setResourcesError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadAgentRuns = useCallback(async () => {
    try {
      setAgentRuns(await fetchJson<AgentRun[]>(`${ORCHESTRATOR_URL}/subtitle/agents/runs?limit=12&include_descendants=true`));
      setAgentRunsError(null);
    } catch (e: unknown) {
      setAgentRunsError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadTasks();
    void loadPublishingTasks();
    void loadResources();
    void loadAgentRuns();
  }, [loadAgentRuns, loadPublishingTasks, loadResources, loadTasks]);

  useEffect(() => {
    fetchJson<ConvertedVideoItem[]>(`${ORCHESTRATOR_URL}/videos/converted?limit=12`)
      .then((data) => setVideos(data))
      .catch((e: unknown) => setVideosError(e instanceof Error ? e.message : String(e)));
  }, []);

  const scheduleAgentListRefresh = useCallback(() => {
    if (agentListTimerRef.current) return;
    agentListTimerRef.current = window.setTimeout(() => {
      agentListTimerRef.current = undefined;
      void loadAgentRuns();
    }, 150);
  }, [loadAgentRuns]);

  const scheduleSelectedAgentRefresh = useCallback((runId: string) => {
    if (!runId || selectedAgentTimerRef.current) return;
    selectedAgentTimerRef.current = window.setTimeout(async () => {
      selectedAgentTimerRef.current = undefined;
      try {
        const run = await fetchJson<AgentRun>(`${ORCHESTRATOR_URL}/subtitle/agents/runs/${runId}`);
        setAgentRuns((current) => {
          const rows = current ?? [];
          const index = rows.findIndex((item) => item.id === run.id);
          if (index < 0) return [run, ...rows];
          const next = [...rows];
          next[index] = run;
          return next;
        });
      } catch (e: unknown) {
        setAgentRunsError(e instanceof Error ? e.message : String(e));
      }
    }, 150);
  }, []);

  const handleRealtimeEvent = useCallback((event: RealtimeEvent) => {
    const data = event.data;
    const id = String(data.id ?? event.entity_id ?? "");
    if (event.name === "system.resources.sample") {
      setResources(data as unknown as ResourceSnapshot);
      setResourcesError(null);
      return;
    }
    if (event.name === "task.updated") {
      const taskId = String(data.id ?? "");
      const trackedUpload = uploadByTaskRef.current.get(taskId);
      const nextTask = {
        ...data,
        ...(uploadByTaskRef.current.has(taskId) ? { bilibili_upload: trackedUpload ?? null } : {}),
      } as unknown as Task;
      setTasks((current) => upsertTask(current, nextTask, 200));
      setPublishingTasks((current) => nextTask.status === "PUBLISHING"
        ? upsertTask(current, nextTask, 6)
        : current?.filter((task) => task.id !== nextTask.id) ?? current);
      return;
    }
    if (event.name === "task.deleted") {
      uploadByTaskRef.current.delete(id);
      setTasks((current) => current?.filter((task) => task.id !== id) ?? current);
      setPublishingTasks((current) => current?.filter((task) => task.id !== id) ?? current);
      return;
    }
    if (event.name === "publish_job.updated" && String(data.platform ?? "") === "bilibili") {
      const taskId = String(data.task_id ?? "");
      uploadByTaskRef.current.set(taskId, data.upload_active
        ? { job_id: String(data.id ?? ""), progress: Math.floor(clampPercent(Number(data.upload_progress ?? 0))) }
        : null);
      setTasks((current) => updateTaskUpload(current, data));
      setPublishingTasks((current) => updateTaskUpload(current, data));
      return;
    }
    if (event.name === "publish_job.deleted") {
      const deleted = { ...data, id, upload_active: false };
      uploadByTaskRef.current.set(String(data.task_id ?? ""), null);
      setTasks((current) => updateTaskUpload(current, deleted));
      setPublishingTasks((current) => updateTaskUpload(current, deleted));
      return;
    }
    if (event.name === "agent_run.started" || event.name === "agent_run.finished") {
      scheduleAgentListRefresh();
      return;
    }
    if (event.name === "agent_run.step_appended") {
      const step = recordValue(data.step);
      if (Object.keys(step).length > 0) {
        setAgentRuns((current) => {
          if (!current) return current;
          const index = current.findIndex((run) => run.id === id);
          if (index < 0) return current;
          const run = current[index];
          const eventId = textValue(step.event_id);
          if (eventId && run.steps.some((item) => textValue(item.event_id) === eventId)) return current;
          const next = [...current];
          next[index] = {
            ...run,
            steps: [...run.steps, step],
            updated_at: event.occurred_at || run.updated_at,
          };
          return next;
        });
      }
      // RAG's normal step event carries only metadata. Keep the old detail
      // refresh for a selected run, while Think deltas render immediately
      // from the full event payload above.
      if (id === selectedAgentNodeId && textValue(step.action) !== "translation_thinking.delta") {
        scheduleSelectedAgentRefresh(id);
      }
    }
  }, [scheduleAgentListRefresh, scheduleSelectedAgentRefresh, selectedAgentNodeId]);

  useRealtimeSubscription(["tasks", "publishing", "resources", "agents"], handleRealtimeEvent, () => {
    void loadTasks();
    void loadPublishingTasks();
    void loadResources();
    void loadAgentRuns();
  });

  useEffect(() => () => {
    if (agentListTimerRef.current) window.clearTimeout(agentListTimerRef.current);
    if (selectedAgentTimerRef.current) window.clearTimeout(selectedAgentTimerRef.current);
  }, []);

  const counts = useMemo(() => {
    const out = new Map<string, number>();
    for (const t of tasks ?? []) out.set(t.status, (out.get(t.status) ?? 0) + 1);
    return Array.from(out.entries()).sort((a, b) => b[1] - a[1]);
  }, [tasks]);

  const total = tasks?.length ?? 0;
  const failedTasks = useMemo(() => (tasks ?? []).filter((t) => t.status === "FAILED").slice(0, 6), [tasks]);
  const runningTasks = useMemo(() => {
    // Uploading is the highest-priority current work.  A task can still have
    // another subtitle/render action attached, so do not rely on task.status
    // alone to decide whether its Bilibili transfer is visible here.
    const liveBilibiliUploads = (tasks ?? []).filter((task) => task.bilibili_upload);
    const latestPublishing = publishingTasks ?? (tasks ?? []).filter((t) => t.status === "PUBLISHING");
    const otherRunning = (tasks ?? []).filter((t) => runningStatuses.has(t.status) && t.status !== "PUBLISHING");
    const seen = new Set<string>();
    return [...liveBilibiliUploads, ...latestPublishing, ...otherRunning]
      .filter((task) => {
        if (seen.has(task.id)) return false;
        seen.add(task.id);
        return true;
      })
      .slice(0, 6);
  }, [publishingTasks, tasks]);
  const agentChildren = useMemo(() => {
    const out = new Map<string, AgentRun[]>();
    for (const run of agentRuns ?? []) {
      if (!run.parent_agent_run_id) continue;
      const rows = out.get(run.parent_agent_run_id) ?? [];
      rows.push(run);
      out.set(run.parent_agent_run_id, rows);
    }
    for (const rows of out.values()) {
      rows.sort((a, b) => new Date(a.started_at).getTime() - new Date(b.started_at).getTime());
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
    () => topLevelAgents.filter((run) => agentTreeDisplayStatus(run, agentChildren).tone === "running"),
    [agentChildren, topLevelAgents],
  );
  const recentFinishedAgents = useMemo(
    () => topLevelAgents.filter((run) => agentTreeDisplayStatus(run, agentChildren).tone !== "running").slice(0, 6),
    [agentChildren, topLevelAgents],
  );
  const selectedAgentRoot = useMemo(() => (selectedAgentRootId ? agentById.get(selectedAgentRootId) ?? null : null), [agentById, selectedAgentRootId]);
  const latestSelectedAgent = useMemo(
    () => (selectedAgentRoot ? latestAgentInTree(selectedAgentRoot, agentChildren) : null),
    [agentChildren, selectedAgentRoot],
  );
  const selectedAgent = useMemo(() => {
    if (!selectedAgentRoot) return null;
    if (followLatestAgent) return latestSelectedAgent ?? selectedAgentRoot;
    return (selectedAgentNodeId ? agentById.get(selectedAgentNodeId) : null) ?? latestSelectedAgent ?? selectedAgentRoot;
  }, [agentById, followLatestAgent, latestSelectedAgent, selectedAgentNodeId, selectedAgentRoot]);
  const openAgentSession = useCallback((run: AgentRun) => {
    let root = run;
    const visited = new Set<string>();
    while (root.parent_agent_run_id && !visited.has(root.id)) {
      visited.add(root.id);
      const parent = agentById.get(root.parent_agent_run_id);
      if (!parent) break;
      root = parent;
    }
    setSelectedAgentRootId(root.id);
    setSelectedAgentNodeId(latestAgentInTree(root, agentChildren).id);
    setFollowLatestAgent(true);
  }, [agentById, agentChildren]);
  useEffect(() => {
    if (!selectedAgentRootId || selectedAgentRoot || !agentRuns) return;
    setSelectedAgentRootId(null);
    setSelectedAgentNodeId(null);
  }, [agentRuns, selectedAgentRoot, selectedAgentRootId]);
  useEffect(() => {
    if (!followLatestAgent || !latestSelectedAgent) return;
    setSelectedAgentNodeId(latestSelectedAgent.id);
  }, [followLatestAgent, latestSelectedAgent]);
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
    <div className="space-y-5">
      <PageHeader
        title="工作台"
        description="优先查看需要处理的任务、实时流水线状态和最新视频成品。"
        actions={
          <>
            <Link to="/videos" className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white">
              视频成品
            </Link>
            <Link to="/tasks/new" className="rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800">
              新建任务
            </Link>
          </>
        }
      />

      {error ? <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

      <section aria-label="任务概览">
        {!tasks ? <div className="vr-section text-sm text-slate-500">任务概览加载中…</div> : null}
        {tasks ? (
          <>
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              <Link to="/tasks" className="group rounded-xl border border-slate-200 bg-white p-4 transition hover:border-slate-300 hover:shadow-sm">
                <div className="text-xs font-medium text-slate-500">全部任务</div>
                <div className="mt-2 flex items-end justify-between gap-2">
                  <div className="text-3xl font-semibold tracking-tight text-slate-950">{total}</div>
                  <span className="text-xs text-slate-400 group-hover:text-slate-600">查看 →</span>
                </div>
              </Link>
              <Link to="/queue/render" className="group rounded-xl border border-sky-200 bg-sky-50 p-4 transition hover:border-sky-300 hover:shadow-sm">
                <div className="text-xs font-medium text-sky-700">正在处理</div>
                <div className="mt-2 flex items-end justify-between gap-2">
                  <div className="text-3xl font-semibold tracking-tight text-sky-950">{runningCount}</div>
                  <span className="text-xs text-sky-600">队列 →</span>
                </div>
              </Link>
              <Link to="/tasks?status=FAILED" className="group rounded-xl border border-rose-200 bg-rose-50 p-4 transition hover:border-rose-300 hover:shadow-sm">
                <div className="text-xs font-medium text-rose-700">需要处理</div>
                <div className="mt-2 flex items-end justify-between gap-2">
                  <div className="text-3xl font-semibold tracking-tight text-rose-950">{failedCount}</div>
                  <span className="text-xs text-rose-600">失败任务 →</span>
                </div>
              </Link>
              <Link to="/tasks?status=PUBLISHED" className="group rounded-xl border border-emerald-200 bg-emerald-50 p-4 transition hover:border-emerald-300 hover:shadow-sm">
                <div className="text-xs font-medium text-emerald-700">已发布</div>
                <div className="mt-2 flex items-end justify-between gap-2">
                  <div className="text-3xl font-semibold tracking-tight text-emerald-950">{publishedCount}</div>
                  <span className="text-xs text-emerald-600">查看 →</span>
                </div>
              </Link>
            </div>

            {counts.length > 0 ? (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <span className="mr-1 text-xs font-medium text-slate-400">状态</span>
                {counts.map(([status, n]) => (
                  <Link
                    key={status}
                    to={`/tasks?status=${status}`}
                    className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 hover:border-slate-300 hover:text-slate-950"
                  >
                    <span>{status}</span>
                    <span className="font-mono text-slate-400">{n}</span>
                  </Link>
                ))}
              </div>
            ) : null}
          </>
        ) : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="vr-section">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950">正在处理</div>
              <div className="mt-1 text-xs text-slate-500">上传、字幕、翻译、渲染与发布中的任务。</div>
            </div>
            <Link to="/queue/render" className="text-xs font-medium text-slate-500 hover:text-slate-950">
              查看队列 →
            </Link>
          </div>
          <div className="mt-3 space-y-2">
            {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
            {tasks && runningTasks.length === 0 ? <div className="text-sm text-slate-500">暂无运行中任务。</div> : null}
            {runningTasks.map((t) => (
              <Link key={t.id} to={`/tasks/${t.id}`} className="block rounded-lg border border-slate-200 p-3 transition hover:border-slate-300 hover:bg-slate-50">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-950">{t.display_title?.trim() || t.source_url || t.id}</div>
                    <div className="mt-1 font-mono text-xs text-slate-500">{t.id.slice(0, 8)} · {formatDate(t.updated_at)}</div>
                  </div>
                  <StatusBadge status={t.status} />
                </div>
                {t.bilibili_upload ? (
                  <div className="mt-3">
                    <div className="flex items-center justify-between gap-3 text-xs text-sky-800">
                      <span>哔哩哔哩上传中</span>
                      <span className="font-mono">{clampPercent(t.bilibili_upload.progress).toFixed(0)}%</span>
                    </div>
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-sky-100">
                      <div className="h-full bg-sky-500 transition-[width] duration-300" style={{ width: `${clampPercent(t.bilibili_upload.progress)}%` }} />
                    </div>
                  </div>
                ) : null}
              </Link>
            ))}
          </div>
        </div>

        <div className="vr-section">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950">最近失败</div>
              <div className="mt-1 text-xs text-slate-500">优先处理会阻塞流水线的任务。</div>
            </div>
            <Link to="/tasks?status=FAILED" className="text-xs font-medium text-slate-500 hover:text-slate-950">
              全部失败 →
            </Link>
          </div>
          <div className="mt-3 space-y-2">
            {!tasks ? <div className="text-sm text-slate-500">加载中…</div> : null}
            {tasks && failedTasks.length === 0 ? <div className="text-sm text-slate-500">暂无失败任务。</div> : null}
            {failedTasks.map((t) => (
              <Link key={t.id} to={`/tasks/${t.id}`} className="block rounded-lg border border-rose-100 p-3 transition hover:border-rose-200 hover:bg-rose-50">
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

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
        <div className="vr-section">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950">Agent 运行</div>
              <div className="mt-1 text-xs text-slate-500">RAG 术语发现、网页读取、总结与字幕翻译思考流。</div>
            </div>
            {agentRuns ? <div className="rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-500">运行中 {runningAgents.length}</div> : null}
          </div>
          {agentRunsError ? <div className="mt-3 text-sm text-rose-700">{agentRunsError}</div> : null}
          {!agentRuns ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
          {agentRuns ? (
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <div>
                <div className="mb-2 text-xs font-semibold text-slate-500">正在工作</div>
                <div className="space-y-2">
                  {runningAgents.length === 0 ? <div className="rounded-lg border border-dashed border-slate-200 p-4 text-sm text-slate-500">暂无运行中的 Agent。</div> : null}
                  {runningAgents.map((run) => <AgentRunCard key={run.id} run={run} childrenByParent={agentChildren} onSelect={openAgentSession} />)}
                </div>
              </div>
              <div>
                <div className="mb-2 text-xs font-semibold text-slate-500">最近完成</div>
                <div className="space-y-2">
                  {recentFinishedAgents.length === 0 ? <div className="rounded-lg border border-dashed border-slate-200 p-4 text-sm text-slate-500">暂无 Agent 结果。</div> : null}
                  {recentFinishedAgents.map((run) => <AgentRunCard key={run.id} run={run} childrenByParent={agentChildren} onSelect={openAgentSession} />)}
                </div>
              </div>
            </div>
          ) : null}
        </div>

        <div className="vr-section">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-950">系统资源</div>
              <div className="mt-1 text-xs text-slate-500">3 秒实时采样</div>
            </div>
            {resources?.sampled_at ? <div className="text-[11px] text-slate-400">{new Date(resources.sampled_at).toLocaleTimeString()}</div> : null}
          </div>
          {resourcesError ? <div className="mt-3 text-sm text-rose-700">{resourcesError}</div> : null}
          {!resources ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
          {resources ? (
            <div className="mt-4 space-y-4">
              <ResourceBar
                label={`CPU${resources.cpu.cores ? ` · ${resources.cpu.cores} 核` : ""}`}
                value={resources.cpu.percent}
                detail={resources.cpu.load_average?.length ? `load ${resources.cpu.load_average.map((n) => n.toFixed(2)).join(" / ")}` : undefined}
              />
              <ResourceBar
                label={resources.cgroup_memory ? "内存 · 容器" : "内存"}
                value={(resources.cgroup_memory ?? resources.memory).percent}
                detail={`${formatBytes((resources.cgroup_memory ?? resources.memory).used_bytes)} / ${formatBytes((resources.cgroup_memory ?? resources.memory).total_bytes)}`}
                tone="emerald"
              />
              {resources.cgroup_memory ? <div className="-mt-2 text-[11px] text-slate-400">主机 {formatBytes(resources.memory.used_bytes)} / {formatBytes(resources.memory.total_bytes)}</div> : null}
              {resources.intel_gpu?.enabled ? (
                <div className="border-t border-slate-100 pt-4">
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
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-950">最新视频成品</div>
            <div className="mt-1 text-xs text-slate-500">最近 12 条 video_final，可继续进入详情、下载或加入播控。</div>
          </div>
          <Link to="/videos" className="text-xs font-medium text-slate-500 hover:text-slate-950">
            管理全部 →
          </Link>
        </div>
        {videosError ? <div className="mt-2 text-sm text-rose-700">{videosError}</div> : null}
        {!videos ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {videos ? (
          <div className="vr-table-wrap mt-4">
            <table className="vr-table">
              <thead>
                <tr>
                  <th>视频</th>
                  <th className="w-24">任务</th>
                  <th className="w-36">状态</th>
                  <th className="w-36 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {videos.map((it) => (
                  <tr key={it.final_asset.id}>
                    <td>
                      <Link to={`/tasks/${it.task.id}`} className="block max-w-[36rem] truncate text-sm font-medium text-slate-950 hover:underline">
                        {fileNameFromKey(it.final_asset.storage_key)}
                      </Link>
                    </td>
                    <td>
                      <Link to={`/tasks/${it.task.id}`} className="font-mono text-xs text-slate-900 hover:underline">
                        {it.task.id.slice(0, 8)}
                      </Link>
                    </td>
                    <td>
                      <StatusBadge status={it.task.status} />
                    </td>
                    <td>
                      <div className="flex items-center justify-end gap-2">
                        <a
                          className="rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-950"
                          href={`${ORCHESTRATOR_URL}/tasks/${it.task.id}/assets/${it.final_asset.id}/download`}
                        >
                          下载
                        </a>
                        <Link className="rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 hover:text-slate-950" to={`/tasks/${it.task.id}`}>
                          详情
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
      {selectedAgentRoot && selectedAgent ? (
        <AgentRunDetail
          rootRun={selectedAgentRoot}
          activeRun={selectedAgent}
          childrenByParent={agentChildren}
          followLatest={followLatestAgent}
          onSelectRun={(run) => {
            setSelectedAgentNodeId(run.id);
            setFollowLatestAgent(false);
          }}
          onToggleFollowLatest={() => setFollowLatestAgent((value) => !value)}
          onClose={() => {
            setSelectedAgentRootId(null);
            setSelectedAgentNodeId(null);
            setFollowLatestAgent(true);
          }}
        />
      ) : null}
    </div>
  );
}
