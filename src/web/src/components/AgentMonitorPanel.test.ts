import { describe, expect, it } from "vitest";
import {
  buildAgentMonitorGroups,
  thinkingText,
  type AgentMonitorRun,
} from "./AgentMonitorPanel.helpers";

function run(overrides: Partial<AgentMonitorRun> = {}): AgentMonitorRun {
  return {
    id: "root",
    agent_type: "subtitle_translation_session",
    status: "running",
    subtitle_job_id: "job",
    subtitle_job_status: "running",
    parent_agent_run_id: null,
    started_at: "2026-10-30T00:00:00Z",
    created_at: "2026-10-30T00:00:00Z",
    updated_at: "2026-10-30T00:00:00Z",
    steps: [],
    result: {},
    ...overrides,
  };
}

describe("AgentMonitorPanel grouping", () => {
  it("shows every running root instead of limiting the monitor to three", () => {
    const rows = Array.from({ length: 6 }, (_, index) =>
      run({ id: "root-" + index, updated_at: "2026-10-30T00:00:0" + index + "Z" })
    );
    expect(buildAgentMonitorGroups(rows).running).toHaveLength(6);
  });

  it("keeps a root visible while a descendant is still running", () => {
    const root = run({ status: "succeeded" });
    const child = run({
      id: "child",
      agent_type: "subtitle_translation_batch",
      parent_agent_run_id: root.id,
      status: "running",
    });
    expect(buildAgentMonitorGroups([root, child]).running.map((item) => item.id)).toEqual(["root"]);
  });
});


describe("provider thinking trace", () => {
  it("renders only explicit reasoning deltas, in order, as bounded plain text", () => {
    const trace = run({ agent_type: "subtitle_translation_thinking", steps: [
      { action: "translation_thinking.delta", output: { thinking_delta: "分析" } },
      { action: "translation_thinking.finished", output: { thinking_delta: "ignore" } },
      { action: "translation_thinking.delta", output: { thinking_delta: "<tag>结果" } },
    ] });
    expect(thinkingText(trace)).toBe("分析<tag>结果");
    expect(thinkingText({ ...trace, agent_type: "rag_master" })).toBe("");
    expect(thinkingText({ ...trace, steps: [{ action: "translation_thinking.delta", output: { thinking_delta: "x".repeat(40_000) } }] }).length).toBe(32_000);
  });
});
