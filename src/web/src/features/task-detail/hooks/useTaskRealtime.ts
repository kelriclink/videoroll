import { useCallback, type Dispatch, type SetStateAction } from "react";
import { useRealtimeSubscription, type RealtimeEvent } from "../../../lib/realtime";
import type { Asset, PublishBatch, PublishJob, SubtitleJob, Task } from "../../../lib/types";
import type { YouTubeDownloadProgress } from "../types";
import { clampUploadProgress, removeById, upsertById } from "../utils";

type UseTaskRealtimeArgs = {
  taskId: string | undefined;
  setTask: Dispatch<SetStateAction<Task | null>>;
  setAssets: Dispatch<SetStateAction<Asset[] | null>>;
  setSubtitleJobs: Dispatch<SetStateAction<SubtitleJob[] | null>>;
  setPublishJobs: Dispatch<SetStateAction<PublishJob[] | null>>;
  setPublishBatches: Dispatch<SetStateAction<PublishBatch[] | null>>;
  setYoutubeDownloadProgress: Dispatch<SetStateAction<YouTubeDownloadProgress | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
  refreshRealtimeSnapshot: () => Promise<void>;
  refreshYoutubeDownloadProgress: () => Promise<void>;
  scheduleLogEventRefresh: () => void;
};

export function useTaskRealtime({
  taskId,
  setTask,
  setAssets,
  setSubtitleJobs,
  setPublishJobs,
  setPublishBatches,
  setYoutubeDownloadProgress,
  setError,
  refreshRealtimeSnapshot,
  refreshYoutubeDownloadProgress,
  scheduleLogEventRefresh,
}: UseTaskRealtimeArgs) {
  const handleRealtimeEvent = useCallback(
    (event: RealtimeEvent) => {
      const data = event.data;
      const id = String(data.id ?? event.entity_id ?? "");
      if (event.name === "task.updated" && id === taskId) {
        setTask((current) => ({ ...(current ?? {}), ...data } as Task));
        return;
      }
      if (event.name === "youtube_download.progress" && String(data.task_id ?? event.entity_id ?? "") === taskId) {
        setYoutubeDownloadProgress(data as unknown as YouTubeDownloadProgress);
        return;
      }
      if (event.name === "task.deleted" && id === taskId) {
        setTask(null);
        setError("任务已被删除");
        return;
      }
      if (event.name === "subtitle_job.updated" && String(data.task_id ?? "") === taskId) {
        setSubtitleJobs((current) => upsertById(current, data as SubtitleJob));
        return;
      }
      if (event.name === "subtitle_job.deleted") {
        setSubtitleJobs((current) => removeById(current, id));
        return;
      }
      if (event.name === "publish_job.updated" && String(data.task_id ?? "") === taskId) {
        const job = data as PublishJob;
        setPublishJobs((current) => upsertById(current, job));
        if (job.platform === "bilibili") {
          setTask((current) => {
            if (!current) return current;
            if (job.upload_active) {
              return { ...current, bilibili_upload: { job_id: job.id, progress: clampUploadProgress(job.upload_progress) } };
            }
            return current.bilibili_upload?.job_id === job.id ? { ...current, bilibili_upload: null } : current;
          });
        }
        return;
      }
      if (event.name === "publish_job.deleted") {
        setPublishJobs((current) => removeById(current, id));
        setTask((current) => current?.bilibili_upload?.job_id === id ? { ...current, bilibili_upload: null } : current);
        return;
      }
      if (event.name === "publish_batch.updated" && String(data.task_id ?? "") === taskId) {
        setPublishBatches((current) => upsertById(current, data as PublishBatch));
        return;
      }
      if (event.name === "publish_batch.deleted") {
        setPublishBatches((current) => removeById(current, id));
        return;
      }
      if (event.name === "asset.updated" && String(data.task_id ?? "") === taskId) {
        setAssets((current) => upsertById(current, data as Asset));
        return;
      }
      if (event.name === "asset.deleted") {
        setAssets((current) => removeById(current, id));
        return;
      }
      if (event.name === "log.updated") scheduleLogEventRefresh();
    },
    [scheduleLogEventRefresh, setAssets, setError, setPublishBatches, setPublishJobs, setSubtitleJobs, setTask, setYoutubeDownloadProgress, taskId],
  );

  useRealtimeSubscription(taskId ? [`task:${taskId}`] : [], handleRealtimeEvent, () => {
    void refreshRealtimeSnapshot();
    void refreshYoutubeDownloadProgress();
  });
}
