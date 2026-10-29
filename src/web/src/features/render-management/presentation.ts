import type { RenderDevice, RenderWorker } from "../../api/renderManagement";

export const ACTIVE_RENDER_STATES = new Set(["claimed", "running", "uploading"]);

export type WorkerDraft = {
  maxConcurrency: number;
  workerKey: string;
};

export function ago(iso?: string | null) {
  if (!iso) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

export function badgeClass(state: string) {
  if (["online", "running", "busy", "succeeded"].includes(state)) {
    return "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300";
  }
  if (["failed", "lost", "canceled", "offline"].includes(state)) {
    return "bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300";
  }
  if (["draining", "paused", "uploading", "cooldown"].includes(state)) {
    return "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300";
  }
  return "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300";
}

export function workerStatusLabel(worker: RenderWorker) {
  if (worker.stale) return "失联";
  if (!worker.enabled) return "已关闭";
  if (worker.draining) return "停止接收新任务";
  if (worker.status === "online") return "在线";
  if (worker.status === "busy") return "忙碌";
  return worker.status || "未知";
}

export function executionStatusLabel(state: string) {
  const labels: Record<string, string> = {
    claimed: "已分配",
    running: "渲染中",
    uploading: "回传中",
    succeeded: "已完成",
    failed: "失败",
    lost: "节点失联",
    canceled: "已取消",
    queued: "等待中",
  };
  return labels[state] ?? "未知";
}

export function deviceStatusLabel(device: RenderDevice) {
  if (device.enabled === false) return "停止分配";
  if (device.status === "cooldown") return "故障冷却";
  if (device.status === "online") return "可用";
  if (device.status === "busy") return "忙碌";
  return device.status || "未知";
}

export function platformLabel(worker: RenderWorker) {
  const gpu = worker.capabilities.gpu_model ?? worker.capabilities.gpu ?? worker.resources.gpu;
  return [worker.platform, worker.architecture, typeof gpu === "string" ? gpu : null]
    .filter(Boolean)
    .join(" · ");
}

export function devicesForWorker(worker: RenderWorker): RenderDevice[] {
  if (Array.isArray(worker.resources.devices)) return worker.resources.devices as RenderDevice[];
  if (Array.isArray(worker.capabilities.devices)) return worker.capabilities.devices as RenderDevice[];
  return [];
}
