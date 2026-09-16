import { useCallback, useEffect, useState } from "react";
import { loadSlices } from "../../../lib/requestState";
import { tasksApi } from "../../../api/tasks";
import { publishApi } from "../../../api/publish";
import type { Asset, PublishBatch, PublishJob, SubtitleJob, Task, TaskCoreSnapshot } from "../../../lib/types";
import type { PublishPlatformSettings, SocialAccount } from "../../../lib/publish";
import type { PublishPlatformSettingsResponse, PublishReview } from "../types";

export function useTaskCore(taskId: string | undefined) {
  const [task, setTask] = useState<Task | null>(null);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [subtitleJobs, setSubtitleJobs] = useState<SubtitleJob[] | null>(null);
  const [publishJobs, setPublishJobs] = useState<PublishJob[] | null>(null);
  const [publishBatches, setPublishBatches] = useState<PublishBatch[] | null>(null);
  const [publishReview, setPublishReview] = useState<PublishReview | null>(null);
  const [socialAccounts, setSocialAccounts] = useState<SocialAccount[]>([]);
  const [publishPlatformSettings, setPublishPlatformSettings] = useState<PublishPlatformSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [coreError, setCoreError] = useState<string | null>(null);
  const [publisherErrors, setPublisherErrors] = useState<Record<string, string>>({});

  const refresh = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!taskId) return;
      const result = await loadSlices<
        TaskCoreSnapshot,
        {
          publishJobs: PublishJob[];
          publishBatches: PublishBatch[];
          publishReview: PublishReview;
          socialAccounts: SocialAccount[];
          publishPlatforms: PublishPlatformSettingsResponse;
        }
      >(
        async () => {
          const [nextTask, nextAssets, nextSubtitleJobs] = await Promise.all([
            tasksApi.get(taskId),
            tasksApi.assets(taskId),
            tasksApi.subtitleJobs(taskId),
          ]);
          return { task: nextTask, assets: nextAssets, subtitleJobs: nextSubtitleJobs };
        },
        {
          publishJobs: () => tasksApi.publishJobs(taskId),
          publishBatches: () => tasksApi.publishBatches(taskId),
          publishReview: () => tasksApi.publishReview(taskId),
          socialAccounts: () => publishApi.accounts(),
          publishPlatforms: () => publishApi.platformSettings(),
        },
      );

      if (result.core.ok) {
        setTask(result.core.value.task);
        setAssets(result.core.value.assets);
        setSubtitleJobs(result.core.value.subtitleJobs);
        setCoreError(null);
      } else {
        setCoreError(result.core.error.message);
      }
      if (result.optional.publishJobs.ok) setPublishJobs(result.optional.publishJobs.value);
      if (result.optional.publishBatches.ok) setPublishBatches(result.optional.publishBatches.value);
      if (result.optional.publishReview.ok) setPublishReview(result.optional.publishReview.value);
      if (result.optional.socialAccounts.ok) setSocialAccounts(result.optional.socialAccounts.value);
      if (result.optional.publishPlatforms.ok) setPublishPlatformSettings(result.optional.publishPlatforms.value.platforms);
      setPublisherErrors(
        Object.fromEntries(Object.entries(result.errors.optional).map(([key, value]) => [key, value?.message ?? "请求失败"])),
      );
      if (!opts?.silent && result.core.ok) setError(null);
    },
    [taskId],
  );

  const refreshRealtimeSnapshot = useCallback(async () => {
    if (!taskId) return;
    const result = await loadSlices<TaskCoreSnapshot, { publishJobs: PublishJob[]; publishBatches: PublishBatch[] }>(
      async () => {
        const [nextTask, nextAssets, nextSubtitleJobs] = await Promise.all([
          tasksApi.get(taskId),
          tasksApi.assets(taskId),
          tasksApi.subtitleJobs(taskId),
        ]);
        return { task: nextTask, assets: nextAssets, subtitleJobs: nextSubtitleJobs };
      },
      {
        publishJobs: () => tasksApi.publishJobs(taskId),
        publishBatches: () => tasksApi.publishBatches(taskId),
      },
    );
    if (result.core.ok) {
      setTask(result.core.value.task);
      setAssets(result.core.value.assets);
      setSubtitleJobs(result.core.value.subtitleJobs);
      setCoreError(null);
    } else {
      setCoreError(result.core.error.message);
    }
    if (result.optional.publishJobs.ok) setPublishJobs(result.optional.publishJobs.value);
    if (result.optional.publishBatches.ok) setPublishBatches(result.optional.publishBatches.value);
  }, [taskId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return {
    task,
    setTask,
    assets,
    setAssets,
    subtitleJobs,
    setSubtitleJobs,
    publishJobs,
    setPublishJobs,
    publishBatches,
    setPublishBatches,
    publishReview,
    setPublishReview,
    socialAccounts,
    publishPlatformSettings,
    error,
    setError,
    coreError,
    publisherErrors,
    refresh,
    refreshRealtimeSnapshot,
  };
}
