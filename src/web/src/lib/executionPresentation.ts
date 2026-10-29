export function queueStateLabel(state: string): string {
  const labels: Record<string, string> = {
    queued: "等待处理",
    running: "处理中",
  };
  return labels[String(state || "").toLowerCase()] ?? "状态未知";
}

export function queueStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    download: "下载素材",
    youtube_download: "下载素材",
    subtitle: "制作字幕",
    translate: "翻译字幕",
    render: "生成视频",
    publish: "发布",
    dubbing: "中文配音",
  };
  return labels[String(stage || "").toLowerCase()] ?? "处理中";
}

export function jobStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "等待中",
    pending: "等待中",
    preparing: "准备中",
    running: "处理中",
    synthesizing: "生成语音",
    aligning: "对齐语音",
    mixing: "混音",
    uploading: "上传中",
    submitting: "提交中",
    submitted: "已提交",
    succeeded: "已完成",
    published: "已发布",
    failed: "失败",
    lost: "节点失联",
    canceled: "已取消",
    cancel_requested: "正在取消",
    pause_requested: "正在暂停",
    paused: "已暂停",
    preview_ready: "预览已就绪",
    needs_review: "需要确认",
    unknown: "需要确认",
  };
  return labels[String(status || "").toLowerCase()] ?? "状态未知";
}

export function publishPlatformLabel(platform?: string | null): string {
  const labels: Record<string, string> = {
    bilibili: "哔哩哔哩",
    douyin: "抖音",
    xiaohongshu: "小红书",
    kuaishou: "快手",
  };
  const value = String(platform || "bilibili").toLowerCase();
  return labels[value] ?? platform ?? "未知平台";
}


export function renderStageLabel(stage?: string | null): string {
  const normalized = String(stage ?? "").toLowerCase();
  if (!normalized) return "";
  if (normalized.includes("upload")) return "回传成品";
  if (normalized.includes("render")) return "生成视频";
  if (normalized.includes("download") || normalized.includes("prepare")) return "准备素材";
  return "处理中";
}
