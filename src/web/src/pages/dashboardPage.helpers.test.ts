import { describe, expect, it } from "vitest";

import { agentRootInactiveReason, knowledgeItemHref, reconcileAgentRunStatuses, type DashboardAgentRunState } from "./dashboardPage.helpers";

describe("knowledgeItemHref", () => {
  it("creates an item query deep link for the knowledge-base page", () => {
    expect(knowledgeItemHref("knowledge item/1")).toBe("/knowledge?item=knowledge+item%2F1");
  });
});

function run(overrides: Partial<DashboardAgentRunState> = {}): DashboardAgentRunState {
  return {
    id: "run-1",
    agent_type: "subtitle_translation_session",
    status: "running",
    subtitle_job_id: "job-1",
    subtitle_job_status: "running",
    parent_agent_run_id: null,
    started_at: "2026-09-20T10:00:00Z",
    created_at: "2026-09-20T10:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

describe("agentRootInactiveReason", () => {
  it("marks a running trace inactive once its subtitle job has ended", () => {
    const item = run({ subtitle_job_status: "succeeded" });
    expect(agentRootInactiveReason(item, [item])).toBe("subtitle_job:succeeded");
  });

  it("marks a running trace inactive when its subtitle job is requeued", () => {
    const item = run({ subtitle_job_status: "queued" });
    expect(agentRootInactiveReason(item, [item])).toBe("subtitle_job:queued");
  });

  it("marks an older translation session inactive after a newer retry starts", () => {
    const older = run({ id: "run-1", started_at: "2026-09-20T10:00:00Z" });
    const newer = run({ id: "run-2", started_at: "2026-09-20T10:05:00Z" });
    expect(agentRootInactiveReason(older, [older, newer])).toBe("superseded");
    expect(agentRootInactiveReason(newer, [older, newer])).toBe("");
  });

  it("keeps a current running session active", () => {
    const item = run();
    expect(agentRootInactiveReason(item, [item])).toBe("");
  });
});

describe("reconcileAgentRunStatuses", () => {
  it("moves an orphaned session and all of its running descendants out of the active state", () => {
    const root = run({ subtitle_job_status: "succeeded" });
    const child = run({
      id: "child-1",
      agent_type: "subtitle_translation_batch",
      parent_agent_run_id: root.id,
      started_at: "2026-09-20T10:01:00Z",
      created_at: "2026-09-20T10:01:00Z",
    });

    const reconciled = reconcileAgentRunStatuses([root, child]);

    expect(reconciled.map((item) => item.status)).toEqual(["interrupted", "interrupted"]);
    expect(reconciled[0].display_status_reason).toBe("subtitle_job:succeeded");
    expect(reconciled[1].display_status_reason).toBe("subtitle_job:succeeded");
  });

  it("does not change a genuinely running session", () => {
    const item = run();
    expect(reconcileAgentRunStatuses([item])[0]).toEqual(item);
  });
});
