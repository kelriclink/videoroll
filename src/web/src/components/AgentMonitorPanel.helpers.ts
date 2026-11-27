import type { DashboardAgentRunState } from "../pages/dashboardPage.helpers";

export type AgentMonitorRun = DashboardAgentRunState & {
  task_id?: string | null;
  term?: string;
  query?: string;
  updated_at: string;
  steps?: Array<Record<string, unknown>>;
  result?: Record<string, unknown>;
  error?: string;
};

function runningInTree(
  run: AgentMonitorRun,
  childrenByParent: Map<string, AgentMonitorRun[]>,
  visited = new Set<string>(),
): boolean {
  if (visited.has(run.id)) return false;
  if (String(run.status || "").toLowerCase() === "running") return true;
  const nextVisited = new Set(visited);
  nextVisited.add(run.id);
  return (childrenByParent.get(run.id) ?? []).some((child) =>
    runningInTree(child, childrenByParent, nextVisited)
  );
}

export function buildAgentMonitorGroups(runs: AgentMonitorRun[]) {
  const childrenByParent = new Map<string, AgentMonitorRun[]>();
  for (const run of runs) {
    if (!run.parent_agent_run_id) continue;
    const children = childrenByParent.get(run.parent_agent_run_id) ?? [];
    children.push(run);
    childrenByParent.set(run.parent_agent_run_id, children);
  }
  for (const children of childrenByParent.values()) {
    children.sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
  }

  const roots = runs
    .filter((run) => !run.parent_agent_run_id)
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const running = roots.filter((run) => runningInTree(run, childrenByParent));
  const runningIds = new Set(running.map((run) => run.id));
  const recent = roots.filter((run) => !runningIds.has(run.id)).slice(0, 6);
  return { childrenByParent, running, recent };
}


// Provider reasoning is data, not markup. Bound client-side accumulation
// even if an older backend returns unusually large trace records.
export function thinkingText(run: AgentMonitorRun): string {
  if (run.agent_type !== "subtitle_translation_thinking") return "";
  let value = "";
  for (const step of run.steps ?? []) {
    if (step.action !== "translation_thinking.delta") continue;
    const output = step.output;
    if (typeof output !== "object" || output === null) continue;
    const delta = (output as Record<string, unknown>).thinking_delta;
    if (typeof delta === "string") value += delta.slice(0, 32_000 - value.length);
    if (value.length >= 32_000) break;
  }
  return value;
}
