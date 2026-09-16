import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { tasksApi } from "../../../api/tasks";
import type { Asset } from "../../../lib/types";
import type { TaskDetailTab } from "../types";

export function useTaskLogs({ taskId, assets, activeTab }: { taskId: string | undefined; assets: Asset[] | null; activeTab: TaskDetailTab }) {
  const [logSelection, setLogSelection] = useState("combined");
  const [logText, setLogText] = useState("");
  const [logBusy, setLogBusy] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const logEventTimerRef = useRef<number | undefined>();
  const lastLogEventFetchAtRef = useRef(0);

  const logAssets = useMemo(() => (assets ?? []).filter((asset) => asset.kind === "log"), [assets]);
  const youtubeDownloadLogAssets = useMemo(() => logAssets.filter((asset) => asset.storage_key.includes("/youtube_download_")), [logAssets]);
  const subtitleLogAssets = useMemo(() => logAssets.filter((asset) => asset.storage_key.includes("/subtitle_")), [logAssets]);
  const renderLogAssets = useMemo(() => logAssets.filter((asset) => asset.storage_key.includes("/render_")), [logAssets]);
  const latestYouTubeDownloadLog = useMemo(
    () => (youtubeDownloadLogAssets.length ? youtubeDownloadLogAssets[youtubeDownloadLogAssets.length - 1] : null),
    [youtubeDownloadLogAssets],
  );
  const latestSubtitleLog = useMemo(
    () => (subtitleLogAssets.length ? subtitleLogAssets[subtitleLogAssets.length - 1] : null),
    [subtitleLogAssets],
  );
  const latestRenderLog = useMemo(() => (renderLogAssets.length ? renderLogAssets[renderLogAssets.length - 1] : null), [renderLogAssets]);
  const selectedLogAsset = useMemo(() => {
    if (logSelection === "combined") return null;
    return logAssets.find((asset) => asset.id === logSelection) ?? null;
  }, [logAssets, logSelection]);

  const loadLogs = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!taskId) return;
      const silent = Boolean(opts?.silent);
      if (!silent) setLogError(null);
      if (!silent) setLogBusy(true);
      try {
        const fetchTail = async (assetId: string, maxBytes: number) => {
          return await tasksApi.assetText(taskId, assetId, maxBytes);
        };

        const maxBytes = 200_000;
        if (logSelection === "combined") {
          const parts: Array<{ title: string; asset: Asset }> = [];
          if (latestYouTubeDownloadLog) parts.push({ title: `YouTube Download · ${latestYouTubeDownloadLog.storage_key}`, asset: latestYouTubeDownloadLog });
          if (latestSubtitleLog) parts.push({ title: `Subtitle · ${latestSubtitleLog.storage_key}`, asset: latestSubtitleLog });
          if (latestRenderLog) parts.push({ title: `Render · ${latestRenderLog.storage_key}`, asset: latestRenderLog });
          if (!parts.length) {
            setLogText("暂无日志（任务开始后会生成 log 资产）");
            return;
          }
          const texts = await Promise.all(parts.map((part) => fetchTail(part.asset.id, maxBytes)));
          const merged = parts.map((part, index) => `===== ${part.title} =====\n${(texts[index] ?? "").trimEnd()}\n`).join("\n");
          setLogText(merged.trimEnd() + "\n");
          return;
        }
        if (!selectedLogAsset) {
          setLogText("日志资产不存在或已被删除");
          return;
        }
        setLogText(await fetchTail(selectedLogAsset.id, maxBytes));
      } catch (error: unknown) {
        if (!silent) setLogError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!silent) setLogBusy(false);
      }
    },
    [taskId, logSelection, latestYouTubeDownloadLog, latestSubtitleLog, latestRenderLog, selectedLogAsset],
  );

  useEffect(() => {
    if (logSelection === "combined" || selectedLogAsset) return;
    setLogSelection("combined");
  }, [logSelection, selectedLogAsset]);

  const logSnapshotKey = [
    logSelection,
    latestYouTubeDownloadLog?.id ?? "",
    latestSubtitleLog?.id ?? "",
    latestRenderLog?.id ?? "",
    selectedLogAsset?.id ?? "",
  ].join("|");

  const scheduleLogEventRefresh = useCallback(() => {
    if (activeTab !== "logs") return;
    const elapsed = Date.now() - lastLogEventFetchAtRef.current;
    const run = () => {
      logEventTimerRef.current = undefined;
      lastLogEventFetchAtRef.current = Date.now();
      void loadLogs({ silent: true });
    };
    if (elapsed >= 2000 && !logEventTimerRef.current) {
      run();
      return;
    }
    if (!logEventTimerRef.current) logEventTimerRef.current = window.setTimeout(run, Math.max(0, 2000 - elapsed));
  }, [activeTab, loadLogs]);

  useEffect(() => {
    if (!taskId || activeTab !== "logs") return;
    lastLogEventFetchAtRef.current = Date.now();
    void loadLogs({ silent: true });
  }, [activeTab, taskId, logSnapshotKey, loadLogs]);

  useEffect(() => () => {
    if (logEventTimerRef.current) window.clearTimeout(logEventTimerRef.current);
    logEventTimerRef.current = undefined;
  }, [taskId]);

  return {
    logSelection,
    setLogSelection,
    logText,
    logBusy,
    logError,
    logAssets,
    selectedLogAsset,
    loadLogs,
    scheduleLogEventRefresh,
  };
}
