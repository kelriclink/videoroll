import { useMemo, useState } from "react";
import type { WorkflowStep, TaskDetailTab } from "./types";
import { useTaskCore } from "./hooks/useTaskCore";
import { useTaskControls } from "./hooks/useTaskControls";
import { useTaskLogs } from "./hooks/useTaskLogs";
import { useTaskMedia } from "./hooks/useTaskMedia";
import { useTaskPublish } from "./hooks/useTaskPublish";
import { useTaskRealtime } from "./hooks/useTaskRealtime";
import { useTaskRender } from "./hooks/useTaskRender";
import { useTaskSubtitle } from "./hooks/useTaskSubtitle";
import { formatRenderFps, formatRenderSpeed, getRenderTelemetry } from "../renderTelemetry";

export function useTaskDetailController(taskId: string | undefined) {
  const [activeTab, setActiveTab] = useState<TaskDetailTab>("overview");
  const [busy, setBusy] = useState(false);
  const core = useTaskCore(taskId);
  const render = useTaskRender(taskId);
  const logs = useTaskLogs({ taskId, assets: core.assets, activeTab });
  const controls = useTaskControls({
    taskId,
    setTask: core.setTask,
    refresh: core.refresh,
    setError: core.setError,
    setBusy,
  });

  const publish = useTaskPublish({
    taskId,
    task: core.task,
    publishJobs: core.publishJobs,
    setPublishReview: core.setPublishReview,
    socialAccounts: core.socialAccounts,
    publishPlatformSettings: core.publishPlatformSettings,
    refresh: core.refresh,
    setError: core.setError,
    setBusy,
  });

  const media = useTaskMedia({
    taskId,
    task: core.task,
    assets: core.assets,
    refresh: core.refresh,
    loadLogs: logs.loadLogs,
    setError: core.setError,
    setBusy,
    publishCoverKey: publish.publishCoverKey,
    setPublishCoverKey: publish.setPublishCoverKey,
    getPublishDraftInput: publish.getPublishDraftInput,
    generatePublishDraft: publish.generatePublishDraft,
  });

  const subtitle = useTaskSubtitle({
    taskId,
    task: core.task,
    rawAsset: media.rawAsset,
    isYouTubeTask: media.isYouTubeTask,
    subtitleJobs: core.subtitleJobs,
    refresh: core.refresh,
    loadLogs: logs.loadLogs,
    downloadYouTubeSource: media.downloadYouTubeSource,
    setError: core.setError,
    setBusy,
    setPublishTypeidMode: publish.setPublishTypeidMode,
  });

  useTaskRealtime({
    taskId,
    setTask: core.setTask,
    setAssets: core.setAssets,
    setSubtitleJobs: core.setSubtitleJobs,
    setPublishJobs: core.setPublishJobs,
    setPublishBatches: core.setPublishBatches,
    setYoutubeDownloadProgress: media.setYoutubeDownloadProgress,
    setError: core.setError,
    refreshRealtimeSnapshot: core.refreshRealtimeSnapshot,
    refreshYoutubeDownloadProgress: media.refreshYoutubeDownloadProgress,
    scheduleLogEventRefresh: logs.scheduleLogEventRefresh,
  });

  const failedSubtitleJobs = useMemo(() => (core.subtitleJobs ?? []).filter((job) => job.status === "failed").length, [core.subtitleJobs]);
  const runningSubtitleJobs = useMemo(
    () => (core.subtitleJobs ?? []).filter((job) => job.status === "queued" || job.status === "running").length,
    [core.subtitleJobs],
  );

  const workflowSteps = useMemo<WorkflowStep[]>(() => {
    if (!core.task) return [];
    const failed = core.task.status === "FAILED";
    const hasSubtitle = media.subtitleAssets.length > 0 || ["SUBTITLE_READY", "RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED"].includes(core.task.status);
    const hasFinalVideo = media.finalAssets.length > 0 || ["RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED"].includes(core.task.status);
    const activeRender = render.activeRenderExecutions[0] ?? null;
    const activeTelemetry = activeRender ? getRenderTelemetry(activeRender) : null;
    const reviewDone = ["APPROVED", "PUBLISHING", "PUBLISHED"].includes(core.task.status);
    const published = core.task.status === "PUBLISHED";
    return [
      { label: "入库", detail: core.task.created_at ? new Date(core.task.created_at).toLocaleString() : "任务已创建", state: "done" },
      {
        label: "获取视频",
        detail: media.rawAsset ? "已获取原始视频" : core.task.source_type === "youtube" ? "等待下载或复用源视频" : "等待上传原始视频",
        state: media.rawAsset ? "done" : failed ? "failed" : "active",
      },
      {
        label: "字幕",
        detail: hasSubtitle ? `${media.subtitleAssets.length || 1} 个字幕产物` : runningSubtitleJobs ? `${runningSubtitleJobs} 个任务运行中` : "等待生成字幕",
        state: hasSubtitle ? "done" : failed && media.rawAsset ? "failed" : media.rawAsset || runningSubtitleJobs ? "active" : "pending",
      },
      {
        label: "渲染",
        detail: hasFinalVideo
          ? `${media.finalAssets.length || 1} 个最终视频`
          : activeTelemetry
            ? `${activeTelemetry.overallProgress}% · ${formatRenderFps(activeTelemetry.fps)} · ${formatRenderSpeed(activeTelemetry.speed)}`
            : hasSubtitle ? "等待压制最终视频" : "等待字幕阶段完成",
        state: hasFinalVideo ? "done" : activeRender ? "active" : failed && hasSubtitle ? "failed" : hasSubtitle ? "active" : "pending",
      },
      {
        label: "审核",
        detail: reviewDone ? "已通过或进入投稿阶段" : hasFinalVideo ? "等待审核或确认投稿信息" : "等待最终视频",
        state: reviewDone ? "done" : failed && hasFinalVideo ? "failed" : hasFinalVideo ? "active" : "pending",
      },
      {
        label: "投稿",
        detail: published ? "已发布" : publish.runningPublishJobs ? `${publish.runningPublishJobs} 个投稿任务运行中` : publish.failedPublishJobs ? `${publish.failedPublishJobs} 个投稿失败` : "等待提交投稿",
        state: published ? "done" : publish.failedPublishJobs ? "failed" : publish.runningPublishJobs ? "active" : reviewDone ? "active" : "pending",
      },
    ];
  }, [
    core.task,
    media.finalAssets,
    media.rawAsset,
    media.subtitleAssets,
    publish.failedPublishJobs,
    publish.runningPublishJobs,
    render.activeRenderExecutions,
    runningSubtitleJobs,
  ]);

  const canResumeSubtitle = subtitle.canResumeSubtitle;
  const nextAction = (() => {
    const task = core.task;
    if (!task) return null;
    if (task.status === "CANCELED") {
      return {
        title: "任务已停止",
        description: "恢复后会从停止前的阶段继续处理。",
        primaryLabel: "恢复任务",
        primaryTone: "primary" as const,
        onPrimary: controls.resumeStoppedTask,
        secondaryLabel: "查看日志",
        onSecondary: () => setActiveTab("logs"),
      };
    }
    if (task.status === "PUBLISHED") {
      return {
        title: "流程已完成",
        description: "任务已经发布。可以回到媒体页下载最终视频，或查看投稿记录。",
        primaryLabel: "查看成品",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("media"),
        secondaryLabel: "投稿记录",
        onSecondary: () => setActiveTab("publish"),
      };
    }
    if (runningSubtitleJobs > 0 || publish.runningPublishJobs > 0) {
      return {
        title: "正在处理",
        description: publish.runningPublishJobs > 0 ? "投稿任务正在提交，日志会自动刷新。" : "字幕或渲染任务正在运行，日志会自动刷新。",
        primaryLabel: "查看日志",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("logs"),
        secondaryLabel: "刷新状态",
        onSecondary: () => core.refresh({ silent: true }),
      };
    }
    if (task.status === "FAILED" && canResumeSubtitle) {
      return {
        title: "任务失败，可继续",
        description: "检测到失败任务或失败字幕作业。优先从已有产物继续，避免重复下载和重复处理。",
        primaryLabel: "从失败处继续",
        primaryTone: "warning" as const,
        onPrimary: () => subtitle.submitSubtitleJob({ resume: true }),
        secondaryLabel: "查看日志",
        onSecondary: () => setActiveTab("logs"),
      };
    }
    if (task.status === "FAILED") {
      return {
        title: "任务失败",
        description: "当前没有可自动继续的字幕作业。先查看日志定位失败阶段。",
        primaryLabel: "查看日志",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("logs"),
        secondaryLabel: "媒体资产",
        onSecondary: () => setActiveTab("media"),
      };
    }
    if (!media.rawAsset) {
      return {
        title: task.source_type === "youtube" ? "获取源视频" : "上传源视频",
        description: task.source_type === "youtube" ? "还没有原始视频资产。先下载 YouTube 视频和元信息。" : "还没有原始视频资产。先上传本地视频文件。",
        primaryLabel: task.source_type === "youtube" ? "去下载视频" : "去上传视频",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("media"),
        secondaryLabel: "查看任务信息",
        onSecondary: () => setActiveTab("overview"),
      };
    }
    if (media.finalAssets.length === 0) {
      return {
        title: "生成字幕和最终视频",
        description: "源视频已就绪。下一步配置字幕、翻译和渲染参数，然后提交处理。",
        primaryLabel: "去生成字幕",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("subtitle"),
        secondaryLabel: "查看资产",
        onSecondary: () => setActiveTab("media"),
      };
    }
    if (core.publishReview?.enabled && core.publishReview.checked && core.publishReview.ok === false) {
      return {
        title: "审核未通过",
        description: core.publishReview.reason || "投稿前审核未通过。请调整标题、简介或审核设置后重试。",
        primaryLabel: "处理投稿信息",
        primaryTone: "warning" as const,
        onPrimary: () => setActiveTab("publish"),
        secondaryLabel: "查看日志",
        onSecondary: () => setActiveTab("logs"),
      };
    }
    return {
      title: "配置并提交投稿",
      description: "最终视频已生成。下一步确认封面、标题、分区和审核结果后提交。",
      primaryLabel: "去投稿",
      primaryTone: "primary" as const,
      onPrimary: () => setActiveTab("publish"),
      secondaryLabel: "下载成品",
      onSecondary: () => setActiveTab("media"),
    };
  })();

  return {
    taskId,
    activeTab,
    setActiveTab,
    busy,
    setBusy,
    ...core,
    ...media,
    ...subtitle,
    ...publish,
    ...logs,
    ...controls,
    ...render,
    failedSubtitleJobs,
    runningSubtitleJobs,
    workflowSteps,
    nextAction,
  };
}

export type TaskDetailController = ReturnType<typeof useTaskDetailController>;
