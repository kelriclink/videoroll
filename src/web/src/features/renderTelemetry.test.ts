import { describe, expect, it } from "vitest";
import type { RenderExecution } from "../api/renderManagement";
import {
  formatRenderDuration,
  formatRenderFps,
  formatRenderSpeed,
  getRenderTelemetry,
  summarizeRenderExecutions,
} from "./renderTelemetry";

const execution = (overrides: Partial<RenderExecution> = {}): RenderExecution => ({
  id: "e1",
  render_job_id: "j1",
  worker_id: "w1",
  attempt: 1,
  state: "running",
  transfer_mode: "http",
  progress: 55,
  render_spec: {},
  metrics: {
    stage: "rendering",
    fps: 83.45,
    speed: 1.5,
    render_percent: 50,
    out_time_seconds: 30,
    duration_seconds: 60,
    elapsed_seconds: 20,
    encoder: "av1_vaapi",
  },
  started_at: "2026-09-22T00:00:00Z",
  ...overrides,
});

describe("render telemetry", () => {
  it("extracts live ffmpeg telemetry and calculates ETA", () => {
    const value = getRenderTelemetry(execution());
    expect(value.fps).toBe(83.45);
    expect(value.speed).toBe(1.5);
    expect(value.renderPercent).toBe(50);
    expect(value.etaSeconds).toBe(20);
    expect(value.encoder).toBe("av1_vaapi");
  });

  it("falls back to coordinator progress when render_percent is absent", () => {
    const value = getRenderTelemetry(execution({
      progress: 55,
      metrics: { fps: 30, speed: 0.5, elapsed_seconds: 70 },
    }));
    expect(value.renderPercent).toBeCloseTo(50);
    expect(value.etaSeconds).toBeCloseTo(70);
  });

  it("formats user-facing values", () => {
    expect(formatRenderFps(83.45)).toBe("83.5 FPS");
    expect(formatRenderSpeed(1.481)).toBe("1.48x");
    expect(formatRenderDuration(65)).toBe("1:05");
    expect(formatRenderDuration(3661)).toBe("1:01:01");
  });

  it("aggregates node throughput across active executions", () => {
    const result = summarizeRenderExecutions([
      execution(),
      execution({ id: "e2", metrics: { fps: 40, speed: 0.75 } }),
    ]);
    expect(result.active).toBe(2);
    expect(result.fps).toBeCloseTo(123.45);
    expect(result.speed).toBeCloseTo(2.25);
  });
});
