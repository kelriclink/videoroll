import type { Task, TaskStatus } from "./types";

export type TaskTone = "neutral" | "info" | "warning" | "danger" | "success";

export type TaskPresentation = {
  label: string;
  shortLabel: string;
  tone: TaskTone;
  inProgress: boolean;
  needsAttention: boolean;
  terminal: boolean;
};

const presentations: Record<TaskStatus, TaskPresentation> = {
  CREATED: { label: "准备中", shortLabel: "准备", tone: "neutral", inProgress: true, needsAttention: false, terminal: false },
  INGESTED: { label: "等待处理", shortLabel: "等待", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  DOWNLOADED: { label: "视频已下载", shortLabel: "已下载", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  AUDIO_EXTRACTED: { label: "准备字幕", shortLabel: "准备字幕", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  ASR_DONE: { label: "正在制作字幕", shortLabel: "字幕处理中", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  TRANSLATED: { label: "正在整理字幕", shortLabel: "字幕处理中", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  SUBTITLE_READY: { label: "字幕已完成", shortLabel: "字幕完成", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  RENDERED: { label: "视频已生成", shortLabel: "已生成", tone: "success", inProgress: true, needsAttention: false, terminal: false },
  READY_FOR_REVIEW: { label: "等待审核", shortLabel: "待审核", tone: "warning", inProgress: false, needsAttention: false, terminal: false },
  APPROVED: { label: "等待发布", shortLabel: "待发布", tone: "warning", inProgress: true, needsAttention: false, terminal: false },
  PUBLISHING: { label: "正在发布", shortLabel: "发布中", tone: "info", inProgress: true, needsAttention: false, terminal: false },
  PUBLISHED: { label: "已发布", shortLabel: "已发布", tone: "success", inProgress: false, needsAttention: false, terminal: true },
  FAILED: { label: "需要处理", shortLabel: "失败", tone: "danger", inProgress: false, needsAttention: true, terminal: true },
  CANCELED: { label: "已停止", shortLabel: "已停止", tone: "neutral", inProgress: false, needsAttention: false, terminal: true },
};

const fallback: TaskPresentation = {
  label: "状态未知",
  shortLabel: "未知",
  tone: "neutral",
  inProgress: false,
  needsAttention: false,
  terminal: false,
};

export function taskPresentation(status: TaskStatus | string): TaskPresentation {
  return presentations[status as TaskStatus] ?? fallback;
}

export function taskDisplayLabel(status: TaskStatus | string): string {
  return taskPresentation(status).label;
}

export function taskIsInProgress(task: Pick<Task, "status">): boolean {
  return taskPresentation(task.status).inProgress;
}

export function taskNeedsAttention(task: Pick<Task, "status">): boolean {
  return taskPresentation(task.status).needsAttention;
}

export type ErrorPresentation = {
  title: string;
  detail: string;
  actionHint?: string;
};

function includesAny(value: string, parts: string[]) {
  return parts.some((part) => value.includes(part));
}

export function presentTaskError(error?: string | null, code?: string | null): ErrorPresentation | null {
  const raw = String(error ?? "").trim();
  const normalized = `${code ?? ""} ${raw}`.toLowerCase();
  if (!normalized.trim()) return null;

  if (includesAny(normalized, ["model_not_found", "no available channel"]) || (normalized.includes("503") && normalized.includes("model"))) {
    return {
      title: "翻译模型当前不可用",
      detail: "上游模型或线路暂时没有可用通道，已完成的处理进度通常可以保留。",
      actionHint: "线路恢复后重新执行即可。",
    };
  }
  if (normalized.includes("number_mismatch")) {
    return {
      title: "翻译数字校验未通过",
      detail: "系统检测到原文与译文中的数字表达可能不一致，任务已停止以避免错误字幕继续流转。",
      actionHint: "可重新执行；若仍失败，再查看技术详情中的字幕位置。",
    };
  }
  if (includesAny(normalized, ["timeout", "timed out", "read operation timed out", "deadline exceeded"])) {
    return {
      title: "上游服务响应超时",
      detail: "请求在规定时间内没有完成，通常属于临时网络或服务拥塞。",
      actionHint: "稍后重试通常可以恢复。",
    };
  }
  if (includesAny(normalized, ["subtitle_input_missing", "input_missing", "source media", "missing input", "找不到输入", "源文件"])) {
    return {
      title: "继续处理所需的源文件已缺失",
      detail: "当前任务没有可用于恢复的原始媒体或中间输入，不能安全地从断点继续。",
      actionHint: "需要重新下载或重新创建源媒体后再执行。",
    };
  }
  if (normalized.includes("503") || normalized.includes("service unavailable")) {
    return {
      title: "上游服务暂时不可用",
      detail: "依赖的外部服务当前返回不可用状态。",
      actionHint: "稍后重试。",
    };
  }

  return {
    title: code ? `任务执行失败（${code}）` : "任务执行失败",
    detail: "系统没有识别出可简化的失败原因，请展开技术详情查看原始错误。",
  };
}
