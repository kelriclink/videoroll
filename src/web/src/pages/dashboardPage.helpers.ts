export function knowledgeItemHref(itemId: string): string {
  return `/knowledge?${new URLSearchParams({ item: itemId }).toString()}`;
}

export type DashboardAgentRunState = {
  id: string;
  agent_type: string;
  status: string;
  subtitle_job_id?: string | null;
  subtitle_job_status?: string | null;
  parent_agent_run_id?: string | null;
  started_at: string;
  created_at: string;
  finished_at?: string | null;
  display_status_reason?: string;
};

function agentRunStartTime(run: DashboardAgentRunState): number {
  const started = Date.parse(run.started_at || "");
  if (Number.isFinite(started)) return started;
  const created = Date.parse(run.created_at || "");
  return Number.isFinite(created) ? created : 0;
}

export function agentRootInactiveReason(
  run: DashboardAgentRunState,
  roots: DashboardAgentRunState[],
): string {
  if ((run.status || "").toLowerCase() !== "running") return "";
  if (run.finished_at) return "finished_at";

  const subtitleJobStatus = (run.subtitle_job_status || "").toLowerCase();
  if (run.subtitle_job_id && subtitleJobStatus && subtitleJobStatus !== "running") {
    return `subtitle_job:${subtitleJobStatus}`;
  }

  if (run.agent_type !== "subtitle_translation_session" || !run.subtitle_job_id) return "";
  const startedAt = agentRunStartTime(run);
  const superseded = roots.some((candidate) => {
    if (candidate.id === run.id) return false;
    if (candidate.parent_agent_run_id) return false;
    if (candidate.agent_type !== "subtitle_translation_session") return false;
    if (candidate.subtitle_job_id !== run.subtitle_job_id) return false;
    const candidateStartedAt = agentRunStartTime(candidate);
    return candidateStartedAt > startedAt
      || (candidateStartedAt === startedAt && candidate.id > run.id);
  });
  return superseded ? "superseded" : "";
}

export function reconcileAgentRunStatuses<T extends DashboardAgentRunState>(runs: T[]): T[] {
  const roots = runs.filter((run) => !run.parent_agent_run_id);
  const childrenByParent = new Map<string, T[]>();
  for (const run of runs) {
    if (!run.parent_agent_run_id) continue;
    const children = childrenByParent.get(run.parent_agent_run_id) ?? [];
    children.push(run);
    childrenByParent.set(run.parent_agent_run_id, children);
  }

  const inactiveReasons = new Map<string, string>();
  const markTree = (run: T, reason: string) => {
    if (inactiveReasons.has(run.id)) return;
    inactiveReasons.set(run.id, reason);
    for (const child of childrenByParent.get(run.id) ?? []) markTree(child, reason);
  };
  for (const root of roots) {
    const existingReason = (root.status || "").toLowerCase() === "interrupted"
      ? String(root.display_status_reason || "interrupted")
      : "";
    const reason = existingReason || agentRootInactiveReason(root, roots);
    if (reason) markTree(root, reason);
  }

  return runs.map((run) => {
    const reason = inactiveReasons.get(run.id);
    if (!reason || (run.status || "").toLowerCase() !== "running") return run;
    return { ...run, status: "interrupted", display_status_reason: reason };
  });
}
