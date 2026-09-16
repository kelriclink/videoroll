import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useConfirm } from "../../../components/feedbackContext";
import { tasksApi, type PlayoutAssetLink } from "../../../api/tasks";
import type { Asset, Task } from "../../../lib/types";
import type { YouTubeDownloadProgress, YouTubeMeta } from "../types";
import { selectTaskAssets } from "../utils";

type DraftInput = { pristine: boolean; meta: any };

type UseTaskMediaArgs = {
  taskId: string | undefined;
  task: Task | null;
  assets: Asset[] | null;
  refresh: (opts?: { silent?: boolean }) => Promise<void>;
  loadLogs: (opts?: { silent?: boolean }) => Promise<void>;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusy: Dispatch<SetStateAction<boolean>>;
  publishCoverKey: string;
  setPublishCoverKey: Dispatch<SetStateAction<string>>;
  getPublishDraftInput: () => DraftInput;
  generatePublishDraft: (mode: "default" | "source", meta?: any) => Promise<void>;
};

export function useTaskMedia({
  taskId,
  task,
  assets,
  refresh,
  loadLogs,
  setError,
  setBusy,
  publishCoverKey,
  setPublishCoverKey,
  getPublishDraftInput,
  generatePublishDraft,
}: UseTaskMediaArgs) {
  const confirm = useConfirm();
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [youtubeMeta, setYoutubeMeta] = useState<YouTubeMeta | null>(null);
  const [youtubeDownloadProgress, setYoutubeDownloadProgress] = useState<YouTubeDownloadProgress | null>(null);
  const [playoutLinks, setPlayoutLinks] = useState<PlayoutAssetLink[] | null>(null);
  const [didAutoPickCover, setDidAutoPickCover] = useState(false);

  const isYouTubeTask = task?.source_type === "youtube" && !!(task.source_url ?? "").trim();
  const { rawAssets, rawAsset, metadataAsset, finalAssets, subtitleAssets, coverAssets } = useMemo(
    () => selectTaskAssets(assets),
    [assets],
  );

  const refreshYoutubeDownloadProgress = useCallback(async () => {
    if (!taskId) return;
    try {
      const progress = await tasksApi.youtubeDownloadProgress(taskId);
      setYoutubeDownloadProgress(progress);
    } catch {
      setYoutubeDownloadProgress(null);
    }
  }, [taskId]);

  useEffect(() => {
    void refreshYoutubeDownloadProgress();
  }, [refreshYoutubeDownloadProgress]);

  useEffect(() => {
    let active = true;
    setCoverFile(null);
    setYoutubeMeta(null);
    setDidAutoPickCover(false);
    setPlayoutLinks(null);
    if (!taskId) return () => undefined;
    void tasksApi.playoutAssets(taskId).then((links) => {
      if (active) setPlayoutLinks(links);
    }).catch(() => {
      if (active) setPlayoutLinks([]);
    });
    return () => {
      active = false;
    };
  }, [taskId]);

  useEffect(() => {
    if (!isYouTubeTask || didAutoPickCover || publishCoverKey || !coverAssets.length) return;
    setPublishCoverKey(coverAssets[coverAssets.length - 1].storage_key);
    setDidAutoPickCover(true);
  }, [isYouTubeTask, didAutoPickCover, publishCoverKey, coverAssets, setPublishCoverKey]);

  const downloadYouTubeSource = useCallback(
    async (opts?: { showProgress?: boolean }) => {
      if (!taskId) return;
      if (opts?.showProgress) {
        setYoutubeDownloadProgress({
          task_id: taskId,
          status: "preparing",
          active: true,
          progress: 0,
          downloaded_bytes: 0,
        });
      }
      const draft = getPublishDraftInput();
      const resp = await tasksApi.youtubeDownload(taskId);
      setYoutubeMeta(resp.metadata);
      if (!publishCoverKey && resp.cover_asset?.storage_key) {
        setPublishCoverKey(resp.cover_asset.storage_key);
        setDidAutoPickCover(true);
      }
      if (draft.pristine) await generatePublishDraft("source", draft.meta);
      await refreshYoutubeDownloadProgress();
      await refresh();
    },
    [taskId, getPublishDraftInput, setYoutubeMeta, publishCoverKey, setPublishCoverKey, generatePublishDraft, refreshYoutubeDownloadProgress, refresh],
  );

  const fetchYouTubeMeta = useCallback(async () => {
    if (!taskId) return null;
    const response = await tasksApi.youtubeMeta(taskId);
    setYoutubeMeta(response.metadata);
    return response.metadata;
  }, [taskId]);

  async function downloadYouTubeVideo() {
    if (!taskId) return;
    setBusy(true);
    setError(null);
    try {
      await downloadYouTubeSource({ showProgress: true });
    } catch (error: unknown) {
      try {
        await refreshYoutubeDownloadProgress();
        await refresh({ silent: true });
        await loadLogs({ silent: true });
      } catch {}
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function uploadVideo() {
    if (!taskId || !videoFile) return;
    setBusy(true);
    setError(null);
    try {
      await tasksApi.uploadVideo(taskId, videoFile);
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function deleteAsset(assetId: string) {
    const ok = await confirm({
      title: "删除最终视频资产",
      message: "确定删除该最终视频资产？这会从存储中删除文件。",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok || !taskId) return;
    setBusy(true);
    setError(null);
    try {
      await tasksApi.deleteAsset(taskId, assetId);
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function uploadCover() {
    if (!taskId || !coverFile) return;
    setBusy(true);
    setError(null);
    try {
      const asset = await tasksApi.uploadCover(taskId, coverFile);
      setPublishCoverKey(asset.storage_key);
      setDidAutoPickCover(true);
      setCoverFile(null);
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  function assetDownloadUrl(assetId: string): string {
    return taskId ? tasksApi.assetDownloadUrl(taskId, assetId) : "#";
  }

  async function addToPlayout(assetId: string) {
    if (!taskId) return;
    setBusy(true);
    setError(null);
    try {
      const link = await tasksApi.addToPlayout(taskId, assetId);
      setPlayoutLinks((current) => [...(current ?? []).filter((item) => item.asset_id !== assetId), link]);
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return {
    videoFile,
    setVideoFile,
    coverFile,
    setCoverFile,
    youtubeDownloadProgress,
    youtubeMeta,
    setYoutubeDownloadProgress,
    didAutoPickCover,
    setDidAutoPickCover,
    isYouTubeTask,
    rawAssets,
    rawAsset,
    metadataAsset,
    finalAssets,
    subtitleAssets,
    coverAssets,
    refreshYoutubeDownloadProgress,
    downloadYouTubeSource,
    fetchYouTubeMeta,
    downloadYouTubeVideo,
    uploadVideo,
    deleteAsset,
    uploadCover,
    assetDownloadUrl,
    playoutLinks,
    addToPlayout,
  };
}
