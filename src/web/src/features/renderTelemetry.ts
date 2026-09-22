import type { RenderExecution } from "../api/renderManagement";

const metricNumber = (metrics: Record<string, unknown>, key: string): number | null => {
  const value = metrics[key];
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(n) ? n : null;
};

const metricText = (metrics: Record<string, unknown>, key: string): string | null => {
  const value = metrics[key];
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text || null;
};

export type RenderTelemetry = {
  stage: string;
  fps: number | null;
  speed: number | null;
  renderPercent: number;
  overallProgress: number;
  frame: number | null;
  outTimeSeconds: number | null;
  durationSeconds: number | null;
  elapsedSeconds: number | null;
  etaSeconds: number | null;
  encoder: string | null;
  pipeline: string | null;
  deviceName: string | null;
  backend: string | null;
};

export function getRenderTelemetry(execution: RenderExecution): RenderTelemetry {
  const metrics = execution.metrics ?? {};
  const speed = metricNumber(metrics, "speed");
  const outTimeSeconds = metricNumber(metrics, "out_time_seconds");
  const durationSeconds = metricNumber(metrics, "duration_seconds");
  const elapsedSeconds = metricNumber(metrics, "elapsed_seconds");
  let renderPercent = metricNumber(metrics, "render_percent");
  if (renderPercent == null) {
    renderPercent = execution.progress >= 20 && execution.progress <= 90
      ? (execution.progress - 20) / 0.7
      : execution.progress >= 90
        ? 100
        : 0;
  }
  renderPercent = Math.max(0, Math.min(100, renderPercent));

  let etaSeconds = metricNumber(metrics, "eta_seconds");
  if (etaSeconds == null && durationSeconds != null && outTimeSeconds != null && speed != null && speed > 0.001) {
    etaSeconds = Math.max(0, (durationSeconds - outTimeSeconds) / speed);
  }
  if (etaSeconds == null && elapsedSeconds != null && renderPercent > 0.1 && renderPercent < 100) {
    etaSeconds = Math.max(0, elapsedSeconds * (100 / renderPercent - 1));
  }

  return {
    stage: metricText(metrics, "stage") ?? execution.state,
    fps: metricNumber(metrics, "fps"),
    speed,
    renderPercent,
    overallProgress: Math.max(0, Math.min(100, Number(execution.progress) || 0)),
    frame: metricNumber(metrics, "frame"),
    outTimeSeconds,
    durationSeconds,
    elapsedSeconds,
    etaSeconds,
    encoder: metricText(metrics, "encoder"),
    pipeline: metricText(metrics, "pipeline"),
    deviceName: metricText(metrics, "device_name"),
    backend: metricText(metrics, "backend"),
  };
}

export function formatRenderFps(value: number | null): string {
  if (value == null || value <= 0) return "—";
  return value >= 100 ? `${value.toFixed(0)} FPS` : `${value.toFixed(1)} FPS`;
}

export function formatRenderSpeed(value: number | null): string {
  if (value == null || value <= 0) return "—";
  return `${value.toFixed(2)}x`;
}

export function formatRenderDuration(value: number | null): string {
  if (value == null || value < 0 || !Number.isFinite(value)) return "—";
  const seconds = Math.round(value);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function summarizeRenderExecutions(executions: RenderExecution[]) {
  const telemetry = executions.map(getRenderTelemetry);
  return {
    fps: telemetry.reduce((sum, item) => sum + (item.fps ?? 0), 0),
    speed: telemetry.reduce((sum, item) => sum + (item.speed ?? 0), 0),
    active: executions.length,
  };
}
