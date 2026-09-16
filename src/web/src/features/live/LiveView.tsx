import { liveApi } from "../../api/live";
import { ChangeEvent, useCallback, useEffect, useMemo, useState, type SetStateAction } from "react";
import { useConfirm, useToast } from "../../components/feedbackContext";
import { Button, EmptyState, PageHeader, Section } from "../../components/ui";

import type {
  LiveAudioControlAction,
  LiveAudioPlaylist,
  LiveDashboard,
  LiveInputSource,
  LiveMedia,
  MediaCandidate,
  PlaybackMode,
  PlaylistItem,
  ResourceTab,
} from "./types";
import { buildLiveAudioControlPayload, formatTime, itemKey, moveItem, sessionClass, sessionLabel } from "./utils";

import { CandidateList, ManualCandidateList, SelectionList } from "./components/LiveSelectionLists";
import { LivePreview } from "./components/LivePreview";

import { LiveAudioPlayer } from "./components/LiveAudioPlayer";

import { TaskVideoPicker } from "./components/TaskVideoPicker";

export default function LiveView() {
  const confirm = useConfirm();
  const toast = useToast();
  const [dashboard, setDashboard] = useState<LiveDashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [settingsDraft, setSettingsDraft] = useState({ rtmpUrl: "", streamKey: "", videoBitrate: 4500, audioBitrate: 160, fps: 30, keyframeSeconds: 2 });
  const [playlistDraft, setPlaylistDraft] = useState({ videos: [] as PlaylistItem[], audios: [] as PlaylistItem[], playbackMode: "sequential" as PlaybackMode, audioPlaybackMode: "sequential" as PlaybackMode, loopPlaylist: true, mixAudio: false });
  const [audioVolumePercent, setAudioVolumePercent] = useState(100);
  const [sourceDraft, setSourceDraft] = useState({ name: "", url: "", editingId: null as string | null });
  const [taskVideoPicker, setTaskVideoPicker] = useState<"completed" | "raw" | null>(null);
  const [taskVideoSelection, setTaskVideoSelection] = useState<PlaylistItem[]>([]);
  const [resourceTab, setResourceTab] = useState<ResourceTab>("video");
  const [selectedVideo, setSelectedVideo] = useState<PlaylistItem | null>(null);
  const [selectedAudio, setSelectedAudio] = useState<PlaylistItem | null>(null);
  const [selectedAudioPlaylistId, setSelectedAudioPlaylistId] = useState<string | null>(null);
  const [newAudioPlaylistName, setNewAudioPlaylistName] = useState("");
  const [renamingVideoId, setRenamingVideoId] = useState<string | null>(null);
  const [videoRenameName, setVideoRenameName] = useState("");

  const rtmpUrl = settingsDraft.rtmpUrl;
  const streamKey = settingsDraft.streamKey;
  const videoBitrate = settingsDraft.videoBitrate;
  const audioBitrate = settingsDraft.audioBitrate;
  const fps = settingsDraft.fps;
  const keyframeSeconds = settingsDraft.keyframeSeconds;
  const setRtmpUrl = (value: string) => setSettingsDraft((current) => ({ ...current, rtmpUrl: value }));
  const setStreamKey = (value: string) => setSettingsDraft((current) => ({ ...current, streamKey: value }));
  const setVideoBitrate = (value: number) => setSettingsDraft((current) => ({ ...current, videoBitrate: value }));
  const setAudioBitrate = (value: number) => setSettingsDraft((current) => ({ ...current, audioBitrate: value }));
  const setFps = (value: number) => setSettingsDraft((current) => ({ ...current, fps: value }));
  const setKeyframeSeconds = (value: number) => setSettingsDraft((current) => ({ ...current, keyframeSeconds: value }));

  const { videos, audios, playbackMode, audioPlaybackMode, loopPlaylist, mixAudio } = playlistDraft;
  const setVideos = useCallback((value: SetStateAction<PlaylistItem[]>) => setPlaylistDraft((current) => ({
    ...current,
    videos: typeof value === "function" ? value(current.videos) : value,
  })), []);
  const setAudios = useCallback((value: SetStateAction<PlaylistItem[]>) => setPlaylistDraft((current) => ({
    ...current,
    audios: typeof value === "function" ? value(current.audios) : value,
  })), []);
  const setPlaybackMode = (value: PlaybackMode) => setPlaylistDraft((current) => ({ ...current, playbackMode: value }));
  const setAudioPlaybackMode = (value: PlaybackMode) => setPlaylistDraft((current) => ({ ...current, audioPlaybackMode: value }));
  const setLoopPlaylist = (value: boolean) => setPlaylistDraft((current) => ({ ...current, loopPlaylist: value }));
  const setMixAudio = (value: boolean) => setPlaylistDraft((current) => ({ ...current, mixAudio: value }));

  const liveSourceName = sourceDraft.name;
  const liveSourceUrl = sourceDraft.url;
  const editingLiveSourceId = sourceDraft.editingId;
  const setLiveSourceName = (value: string) => setSourceDraft((current) => ({ ...current, name: value }));
  const setLiveSourceUrl = (value: string) => setSourceDraft((current) => ({ ...current, url: value }));
  const setEditingLiveSourceId = (value: string | null) => setSourceDraft((current) => ({ ...current, editingId: value }));

  const refresh = useCallback(async () => {
    try {
      const next = await liveApi.dashboard();
      setDashboard(next);
      setRtmpUrl(next.settings.rtmp_url);
      setVideoBitrate(next.settings.video_bitrate_kbps);
      setAudioBitrate(next.settings.audio_bitrate_kbps);
      setFps(next.settings.fps);
      setKeyframeSeconds(next.settings.keyframe_interval_seconds);
      setVideos(next.playlist.video_items);
      setAudios(next.playlist.audio_items);
      setPlaybackMode(next.playlist.playback_mode);
      setAudioPlaybackMode(["starting", "running", "paused"].includes(next.session.status) ? next.session.audio_playback_mode : next.playlist.audio_playback_mode);
      setAudioVolumePercent(next.session.audio_volume_percent);
      setLoopPlaylist(next.playlist.loop_playlist);
      setMixAudio(["starting", "running", "paused"].includes(next.session.status) ? next.session.mix_audio : next.playlist.mix_audio);
      const availableVideos: PlaylistItem[] = [
        ...next.library_media.filter((media) => media.media_type === "video").map((media) => ({ source: "library" as const, id: media.id })),
        ...next.live_sources.map((source) => ({ source: "live_source" as const, id: source.id })),
      ];
      const availableAudio: PlaylistItem[] = next.library_media
        .filter((media) => media.media_type === "audio")
        .map((media) => ({ source: "library" as const, id: media.id }));
      setSelectedVideo((current) => {
        if (current && availableVideos.some((item) => itemKey(item) === itemKey(current))) return current;
        const active = next.session.current_video;
        return active && availableVideos.some((item) => itemKey(item) === itemKey(active))
          ? { source: active.source, id: active.id }
          : availableVideos[0] ?? null;
      });
      setSelectedAudio((current) => {
        if (current && availableAudio.some((item) => itemKey(item) === itemKey(current))) return current;
        const active = next.session.current_audio;
        return active && availableAudio.some((item) => itemKey(item) === itemKey(active))
          ? { source: active.source, id: active.id }
          : availableAudio[0] ?? null;
      });
      setSelectedAudioPlaylistId((current) => current && next.audio_playlists.some((playlist) => playlist.id === current) ? current : null);
      setError(null);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [setAudios, setVideos]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!dashboard || !["starting", "running", "paused"].includes(dashboard.session.status)) return;
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [dashboard, refresh]);

  const videoResourceCandidates = useMemo<MediaCandidate[]>(() => {
    if (!dashboard) return [];
    return dashboard.library_media
      .filter((media) => media.media_type === "video")
      .map((media) => ({
        item: { source: "library" as const, id: media.id },
        displayName: media.display_name,
        subtitle: media.origin === "completed_video"
          ? `成品副本 · 来源任务 ${media.source_task_id?.slice(0, 8) || "未知"}`
          : media.origin === "raw_video"
            ? `原视频副本 · 来源任务 ${media.source_task_id?.slice(0, 8) || "未知"}`
            : `手动上传 · ${media.storage_key}`,
        sizeBytes: media.size_bytes,
      }));
  }, [dashboard]);

  const completedVideoCandidates = useMemo<MediaCandidate[]>(() => (dashboard?.completed_videos ?? []).map((video) => ({
    item: { source: "task_asset" as const, id: video.id },
    displayName: video.display_name,
    subtitle: `成品视频 · 任务 ${video.task_id.slice(0, 8)} · ${formatTime(video.created_at)}`,
    sizeBytes: video.size_bytes,
  })), [dashboard]);

  const rawVideoCandidates = useMemo<MediaCandidate[]>(() => (dashboard?.raw_videos ?? []).map((video) => ({
    item: { source: "task_asset" as const, id: video.id },
    displayName: video.display_name,
    subtitle: `原视频 · 任务 ${video.task_id.slice(0, 8)} · ${formatTime(video.created_at)}`,
    sizeBytes: video.size_bytes,
  })), [dashboard]);

  const liveSourceCandidates = useMemo<MediaCandidate[]>(() => {
    if (!dashboard) return [];
    return dashboard.live_sources.map((source) => ({
      item: { source: "live_source" as const, id: source.id },
      displayName: source.display_name,
      subtitle: source.url,
      sizeLabel: "实时直播流",
    }));
  }, [dashboard]);

  const videoCandidates = useMemo(
    () => [...videoResourceCandidates, ...completedVideoCandidates, ...rawVideoCandidates, ...liveSourceCandidates],
    [completedVideoCandidates, liveSourceCandidates, rawVideoCandidates, videoResourceCandidates],
  );

  const audioCandidates = useMemo<MediaCandidate[]>(() => {
    if (!dashboard) return [];
    return dashboard.library_media
      .filter((media) => media.media_type === "audio")
      .map((media) => ({
        item: { source: "library" as const, id: media.id },
        displayName: media.display_name,
        subtitle: `手动上传 · ${media.storage_key}`,
        sizeBytes: media.size_bytes,
      }));
  }, [dashboard]);

  const selectedAudioPlaylist = useMemo(
    () => dashboard?.audio_playlists.find((playlist) => playlist.id === selectedAudioPlaylistId) ?? null,
    [dashboard, selectedAudioPlaylistId],
  );
  const selectedAudioPlaylistCandidates = useMemo(
    () => selectedAudioPlaylist
      ? audioCandidates.filter((candidate) => selectedAudioPlaylist.audio_media_ids.includes(candidate.item.id))
      : [],
    [audioCandidates, selectedAudioPlaylist],
  );
  const audioPlayerCandidates = selectedAudioPlaylist ? selectedAudioPlaylistCandidates : audioCandidates;
  const videoCandidateMap = useMemo(() => new Map(videoCandidates.map((candidate) => [itemKey(candidate.item), candidate])), [videoCandidates]);
  const audioCandidateMap = useMemo(() => new Map(audioCandidates.map((candidate) => [itemKey(candidate.item), candidate])), [audioCandidates]);
  const selectedVideoCandidate = selectedVideo ? videoCandidateMap.get(itemKey(selectedVideo)) ?? null : null;
  const selectedAudioCandidate = selectedAudio ? audioCandidateMap.get(itemKey(selectedAudio)) ?? null : null;
  const isActive = Boolean(dashboard && ["starting", "running", "paused"].includes(dashboard.session.status));
  const session = dashboard?.session;
  const isLiveRunning = session?.status === "running";
  function toggleItem(setter: (items: PlaylistItem[]) => void, items: PlaylistItem[], candidate: MediaCandidate, checked: boolean) {
    if (checked) setter([...items, candidate.item]);
    else setter(items.filter((item) => itemKey(item) !== itemKey(candidate.item)));
  }

  function openTaskVideoPicker(kind: "completed" | "raw") {
    setTaskVideoSelection([]);
    setTaskVideoPicker(kind);
  }

  async function applyTaskVideoSelection() {
    if (!taskVideoPicker || taskVideoSelection.length === 0 || busy !== null) return;
    setBusy("import-video");
    try {
      const imported = await liveApi.importMedia(taskVideoSelection.map((item) => item.id));
      const importedIds = new Set(imported.map((media) => media.id));
      setDashboard((current) => current ? {
        ...current,
        library_media: [...imported, ...current.library_media.filter((media) => !importedIds.has(media.id))],
      } : current);
      if (imported[0]) setSelectedVideo({ source: "library", id: imported[0].id });
      setVideos((current) => {
        const selectedAssetIds = new Set(taskVideoSelection.map((item) => item.id));
        const next = current.filter((item) => item.source !== "task_asset" || !selectedAssetIds.has(item.id));
        for (const media of imported) {
          const item: PlaylistItem = { source: "library", id: media.id };
          if (!next.some((existing) => itemKey(existing) === itemKey(item))) next.push(item);
        }
        return next;
      });
      toast({
        kind: "success",
        title: `已导入 ${imported.length} 个视频到直播媒体库`,
        message: "资源已复制到独立直播存储；请保存播放列表以保留播放顺序。",
      });
      setError(null);
      setTaskVideoPicker(null);
      setTaskVideoSelection([]);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings(clearKey = false) {
    setBusy("settings");
    try {
      const payload: Record<string, unknown> = {
        rtmp_url: rtmpUrl,
        video_bitrate_kbps: videoBitrate,
        audio_bitrate_kbps: audioBitrate,
        fps,
        keyframe_interval_seconds: keyframeSeconds,
      };
      if (streamKey.trim() || clearKey) payload.stream_key = clearKey ? "" : streamKey.trim();
      const settings = await liveApi.updateSettings(payload);
      setStreamKey("");
      setDashboard((current) => (current ? { ...current, settings } : current));
      toast({ kind: "success", title: "直播设置已保存", message: settings.stream_key_set ? "推流码已加密保存。" : "尚未设置推流码。" });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function clearStreamKey() {
    const ok = await confirm({ title: "清除推流码", message: "清除后无法开始新的直播，确定继续吗？", confirmLabel: "清除", tone: "danger" });
    if (ok) await saveSettings(true);
  }

  async function savePlaylist() {
    setBusy("playlist");
    try {
      const playlist = await liveApi.updatePlaylist({
        video_items: videos,
        audio_items: audios,
        playback_mode: playbackMode,
        audio_playback_mode: audioPlaybackMode,
        loop_playlist: loopPlaylist,
        mix_audio: mixAudio,
      });
      setDashboard((current) => (current ? { ...current, playlist } : current));
      toast({ kind: "success", title: "播放列表已保存" });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function uploadMedia(mediaType: "video" | "audio", event: ChangeEvent<HTMLInputElement>, audioPlaylistId?: string) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (files.length === 0) return;
    setBusy(`upload-${mediaType}`);
    try {
      const form = new FormData();
      for (const file of files) form.append("files", file, file.name);
      if (mediaType === "audio" && audioPlaylistId) form.append("audio_playlist_id", audioPlaylistId);
      const media = await liveApi.uploadMedia(mediaType, form);
      if (mediaType === "video" && media[0]) setSelectedVideo({ source: "library", id: media[0].id });
      if (mediaType === "audio" && media[0]) setSelectedAudio({ source: "library", id: media[0].id });
      toast({
        kind: "success",
        title: `${mediaType === "video" ? "视频" : "歌曲"}已加入直播资源库`,
        message: `已上传 ${media.length} 个文件。`,
      });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function deleteMedia(media: LiveMedia) {
    const ok = await confirm({ title: "删除直播媒体", message: `确定删除“${media.display_name}”吗？`, confirmLabel: "删除", tone: "danger" });
    if (!ok) return;
    setBusy(`delete-${media.id}`);
    try {
      await liveApi.deleteMedia(media.id);
      if (selectedVideo?.source === "library" && selectedVideo.id === media.id) setSelectedVideo(null);
      if (selectedAudio?.source === "library" && selectedAudio.id === media.id) setSelectedAudio(null);
      toast({ kind: "success", title: "直播媒体已删除" });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function createAudioPlaylist() {
    const displayName = newAudioPlaylistName.trim();
    if (!displayName) {
      setError("请填写歌单名称");
      return;
    }
    setBusy("audio-playlist-create");
    try {
      const playlist = await liveApi.createAudioPlaylist(displayName);
      setNewAudioPlaylistName("");
      setSelectedAudioPlaylistId(playlist.id);
      toast({ kind: "success", title: "歌单已创建", message: playlist.display_name });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function deleteAudioPlaylist(playlist: LiveAudioPlaylist) {
    const ok = await confirm({
      title: "删除歌单",
      message: `删除“${playlist.display_name}”只会移除歌单分组，歌曲文件会保留在音频库。`,
      confirmLabel: "删除歌单",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(`audio-playlist-delete-${playlist.id}`);
    try {
      await liveApi.deleteAudioPlaylist(playlist.id);
      if (selectedAudioPlaylistId === playlist.id) setSelectedAudioPlaylistId(null);
      toast({ kind: "success", title: "歌单已删除", message: "歌曲仍保留在音频库。" });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function saveVideoRename(media: LiveMedia) {
    const displayName = videoRenameName.trim();
    if (!displayName) {
      setError("请填写视频名称");
      return;
    }
    setBusy(`video-rename-${media.id}`);
    try {
      const updated = await liveApi.renameMedia(media.id, displayName);
      setDashboard((current) => current ? {
        ...current,
        library_media: current.library_media.map((item) => item.id === updated.id ? updated : item),
      } : current);
      setRenamingVideoId(null);
      setVideoRenameName("");
      toast({ kind: "success", title: "视频名称已更新", message: updated.display_name });
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  function resetLiveSourceForm() {
    setEditingLiveSourceId(null);
    setLiveSourceName("");
    setLiveSourceUrl("");
  }

  function editLiveSource(source: LiveInputSource) {
    setEditingLiveSourceId(source.id);
    setLiveSourceName(source.display_name);
    setLiveSourceUrl(source.url);
  }

  async function saveLiveSource() {
    if (!liveSourceUrl.trim()) {
      setError("请填写直播源地址");
      return;
    }
    setBusy(editingLiveSourceId ? `source-edit-${editingLiveSourceId}` : "source-create");
    try {
      const sourcePayload = { url: liveSourceUrl.trim(), display_name: liveSourceName.trim() || null };
      const source = editingLiveSourceId
        ? await liveApi.updateSource(editingLiveSourceId, sourcePayload)
        : await liveApi.createSource(sourcePayload);
      if (!editingLiveSourceId) setSelectedVideo({ source: "live_source", id: source.id });
      toast({ kind: "success", title: editingLiveSourceId ? "直播源已更新" : "直播源已添加" });
      resetLiveSourceForm();
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function deleteLiveSource(source: LiveInputSource) {
    const ok = await confirm({ title: "删除直播源", message: `确定删除“${source.display_name}”吗？`, confirmLabel: "删除", tone: "danger" });
    if (!ok) return;
    setBusy(`source-delete-${source.id}`);
    try {
      await liveApi.deleteSource(source.id);
      if (selectedVideo?.source === "live_source" && selectedVideo.id === source.id) setSelectedVideo(null);
      if (editingLiveSourceId === source.id) resetLiveSourceForm();
      toast({ kind: "success", title: "直播源已删除" });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function playSelectedResources() {
    if (!selectedVideo) {
      setError("请先选择一个视频或直播源");
      return;
    }
    setBusy("play-selected");
    try {
      const session = await liveApi.play({ video_item: selectedVideo, audio_item: selectedAudio, mix_audio: mixAudio });
      setDashboard((current) => current ? { ...current, session } : current);
      toast({
        kind: "success",
        title: isActive ? "已切换推流资源" : "已开始选中资源推流",
        message: selectedAudio
          ? (mixAudio ? "已混合视频原声与所选音频。" : "已使用所选独立音频。")
          : "使用视频原声。",
      });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function controlLiveAudio(
    action: LiveAudioControlAction,
    positionSeconds?: number,
    setting?: PlaybackMode | number,
    audioItemOverride?: PlaylistItem,
  ) {
    if (!isLiveRunning) {
      setError("请先开始直播，再控制独立音频。");
      return;
    }
    const audioItem = audioItemOverride ?? selectedAudio;
    if (action === "play" && !audioItem) {
      setError("请先在音频库选择一首歌曲。");
      return;
    }
    const queue = audioPlayerCandidates.map((candidate) => candidate.item);
    if ((action === "previous" || action === "next") && queue.length === 0) {
      setError("当前歌单没有可播放的歌曲。");
      return;
    }
    setBusy(`audio-${action}`);
    try {
      const payload = buildLiveAudioControlPayload({
        action,
        audioItem,
        queue,
        playbackMode: audioPlaybackMode,
        positionSeconds,
        currentPositionSeconds: session?.audio_position_seconds,
        setting,
        volumePercent: audioVolumePercent,
      });
      const nextSession = await liveApi.audioControl(payload as import("../../api/live").LiveAudioControlRequest);
      setDashboard((current) => current ? { ...current, session: nextSession } : current);
      if (action === "play" && audioItem) {
        setSelectedAudio(audioItem);
      }
      if (action === "set_playback_mode") setAudioPlaybackMode((setting ?? audioPlaybackMode) as PlaybackMode);
      if (action === "set_volume") setAudioVolumePercent(Math.max(0, Math.min(200, Number(setting ?? audioVolumePercent))));
      setError(null);
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  async function runAction(action: "start" | "pause" | "resume" | "stop") {
    if (action === "start") {
      const ok = await confirm({
        title: "开始 RTMP 推流",
        message: "将立即向已保存的 RTMP 地址发送视频。请确认你拥有所选媒体及推流账号的使用权限。",
        confirmLabel: "开始推流",
        tone: "warning",
      });
      if (!ok) return;
    }
    if (action === "stop") {
      const ok = await confirm({ title: "停止直播", message: "将停止 FFmpeg 推流进程。", confirmLabel: "停止", tone: "danger" });
      if (!ok) return;
    }
    setBusy(action);
    try {
      const session = await liveApi.action(action);
      setDashboard((current) => (current ? { ...current, session } : current));
      toast({ kind: "success", title: { start: "推流已启动", pause: "推流已暂停", resume: "推流已恢复", stop: "推流已停止" }[action] });
      await refresh();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  }

  const settingsReady = Boolean(dashboard?.settings.rtmp_url && dashboard?.settings.stream_key_set);
  const canStart = !isActive && settingsReady && videos.length > 0;
  const selectedVideoIsCurrent = Boolean(
    selectedVideo && session?.current_video && itemKey(selectedVideo) === itemKey(session.current_video),
  );
  const playerActionLabel = busy === "play-selected"
    ? "正在切换…"
    : !isActive
      ? "使用选中资源开始推流"
      : selectedVideoIsCurrent
        ? "应用音频设置"
        : "切换到选中视频";

  return (
    <>
    <div className="space-y-4">
      <PageHeader
        title="直播推流"
        description="通过 FFmpeg 向 RTMP/RTMPS 平台推流。推流地址与推流码分开保存，推流码仅以加密形式保存在服务器。"
        actions={<Button disabled={busy !== null} onClick={() => void refresh()}>刷新状态</Button>}
      />

      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}</div> : null}

      <Section>
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="text-base font-semibold text-slate-950">直播控制台</div>
            <div className="mt-1 text-sm text-slate-600">建议优先使用 RTMPS。输出固定为 H.264 视频、AAC 音频、FLV 封装，以兼容大多数直播平台。</div>
          </div>
          <div className={`inline-flex rounded-full border px-3 py-1 text-sm font-medium ${sessionClass(session?.status ?? "idle")}`}>
            {sessionLabel(session?.status ?? "idle")}
          </div>
        </div>

        <div className="mt-5 grid gap-4 lg:grid-cols-2">
          <label className="block text-sm font-medium text-slate-800">
            RTMP 推流地址
            <input value={rtmpUrl} disabled={isActive} onChange={(event) => setRtmpUrl(event.target.value)} placeholder="rtmps://live.example.com/app" className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm outline-none focus:border-slate-700 disabled:bg-slate-100" />
          </label>
          <label className="block text-sm font-medium text-slate-800">
            推流码 / Stream Key
            <input value={streamKey} disabled={isActive} type="password" autoComplete="new-password" onChange={(event) => setStreamKey(event.target.value)} placeholder={dashboard?.settings.stream_key_set ? "已保存；留空则不修改" : "例如平台提供的 stream key"} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm outline-none focus:border-slate-700 disabled:bg-slate-100" />
            <span className="mt-1 block text-xs text-slate-500">{dashboard?.settings.stream_key_set ? "已加密保存。" : "尚未保存推流码。"}</span>
          </label>
          <label className="block text-sm font-medium text-slate-800">
            视频码率（kbps）
            <input type="number" min={500} max={20000} disabled={isActive} value={videoBitrate} onChange={(event) => setVideoBitrate(Number(event.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" />
          </label>
          <label className="block text-sm font-medium text-slate-800">
            音频码率（kbps）
            <input type="number" min={32} max={512} disabled={isActive} value={audioBitrate} onChange={(event) => setAudioBitrate(Number(event.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" />
          </label>
          <label className="block text-sm font-medium text-slate-800">
            帧率（FPS）
            <select disabled={isActive} value={fps} onChange={(event) => setFps(Number(event.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100">
              <option value={24}>24</option><option value={25}>25</option><option value={30}>30</option><option value={50}>50</option><option value={60}>60</option>
            </select>
          </label>
          <label className="block text-sm font-medium text-slate-800">
            关键帧间隔（秒）
            <select disabled={isActive} value={keyframeSeconds} onChange={(event) => setKeyframeSeconds(Number(event.target.value))} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100">
              <option value={1}>1</option><option value={2}>2（推荐）</option><option value={4}>4</option><option value={6}>6</option>
            </select>
          </label>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button tone="primary" disabled={isActive || busy !== null} onClick={() => void saveSettings()}>{busy === "settings" ? "保存中..." : "保存直播设置"}</Button>
          {dashboard?.settings.stream_key_set ? <Button tone="danger" disabled={isActive || busy !== null} onClick={() => void clearStreamKey()}>清除推流码</Button> : null}
          <div className="flex-1" />
          {session?.status === "paused" ? (
            <Button tone="primary" disabled={busy !== null} onClick={() => void runAction("resume")}>{busy === "resume" ? "恢复中..." : "恢复推流"}</Button>
          ) : (
            <Button tone="primary" disabled={!canStart || busy !== null} onClick={() => void runAction("start")}>{busy === "start" ? "启动中..." : "开始直播"}</Button>
          )}
          <Button tone="warning" disabled={session?.status !== "running" || busy !== null} onClick={() => void runAction("pause")}>{busy === "pause" ? "暂停中..." : "暂停推流"}</Button>
          <Button tone="danger" disabled={!isActive || busy !== null} onClick={() => void runAction("stop")}>{busy === "stop" ? "停止中..." : "停止直播"}</Button>
        </div>
        <div className="mt-4 grid gap-2 rounded-md bg-slate-50 p-3 text-xs text-slate-600 sm:grid-cols-2">
          <div>当前视频：{session?.current_video?.display_name ?? "-"}</div>
          <div>当前音频：{session?.current_audio?.display_name ?? "使用视频原声"}</div>
          <div>开始时间：{formatTime(session?.started_at)}</div>
          <div>最近更新：{formatTime(session?.updated_at)}</div>
          {session?.last_error ? <div className="sm:col-span-2 text-rose-700">错误：{session.last_error}</div> : null}
        </div>
        <div className="mt-3 text-xs text-slate-500">暂停会结束当前 FFmpeg 推流连接；恢复时会从当前视频开头重新推送，避免长时间冻结 RTMP 连接导致平台断流。</div>
      </Section>

      <Section>
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(20rem,0.85fr)]">
          <LivePreview active={session?.status === "starting" || session?.status === "running"} />
          <div className="rounded-lg border border-slate-800 bg-slate-950 p-4 text-slate-100 shadow-inner">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">播放控制</div>
                <div className="mt-1 text-xs text-slate-400">选中资源后点击播放；切换背景音频不会重新连接 RTMP，也不会重启当前视频。</div>
              </div>
              <span className={isActive ? "text-xs text-emerald-300" : "text-xs text-slate-500"}>{isActive ? "混流器运行中" : "等待开始"}</span>
            </div>
            <div className="mt-5 space-y-3">
              <div className="rounded-md border border-slate-700 bg-slate-900 px-3 py-3">
                <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">视频 / 直播源</div>
                <div className="mt-1 truncate text-sm font-medium">{selectedVideoCandidate?.displayName ?? "尚未选择"}</div>
                <div className="mt-1 truncate text-xs text-slate-400">{selectedVideoCandidate?.subtitle ?? "请在资源面板选择视频或直播源"}</div>
              </div>
              <div className="rounded-md border border-slate-700 bg-slate-900 px-3 py-3">
                <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">独立音频</div>
                <div className="mt-1 truncate text-sm font-medium">{selectedAudioCandidate?.displayName ?? "使用视频原声"}</div>
                <div className="mt-1 truncate text-xs text-slate-400">{selectedAudio ? (mixAudio ? "与视频原声混合" : "替换视频原声") : "未选择背景音频"}</div>
              </div>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <Button tone="primary" disabled={!selectedVideo || busy !== null || session?.status === "paused"} onClick={() => void playSelectedResources()}>
                {playerActionLabel}
              </Button>
              <Button
                disabled={busy !== null || (!isLiveRunning && !selectedAudio)}
                onClick={() => {
                  if (isLiveRunning) void controlLiveAudio("original");
                  else setSelectedAudio(null);
                }}
              >
                {isLiveRunning ? "立即切到视频原声" : "仅用视频原声"}
              </Button>
            </div>
          </div>
        </div>
      </Section>

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-base font-semibold text-slate-950">播放策略</div>
            <div className="mt-1 text-sm text-slate-600">视频为必选资源；音频可选，未选择独立音频时保留视频原声。</div>
          </div>
          <Button tone="primary" disabled={isActive || busy !== null} onClick={() => void savePlaylist()}>{busy === "playlist" ? "保存中..." : "保存播放列表"}</Button>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-3">
          <label className="block text-sm font-medium text-slate-800">
            播放方式
            <select disabled={isActive} value={playbackMode} onChange={(event) => setPlaybackMode(event.target.value as PlaybackMode)} className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100">
              <option value="sequential">按列表顺序</option>
              <option value="shuffle">随机播放</option>
            </select>
          </label>
          <label className="flex items-center gap-3 rounded-md border border-slate-200 px-3 py-3 text-sm text-slate-800">
            <input type="checkbox" checked={loopPlaylist} disabled={isActive} onChange={(event) => setLoopPlaylist(event.target.checked)} />
            播放列表结束后循环播放
          </label>
          <label className="flex items-center gap-3 rounded-md border border-slate-200 px-3 py-3 text-sm text-slate-800">
            <input type="checkbox" checked={mixAudio} disabled={busy !== null} onChange={(event) => setMixAudio(event.target.checked)} />
            混合视频原声与独立音频
          </label>
        </div>
      </Section>

      <Section>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-base font-semibold text-slate-950">资源与播放列表</div>
            <div className="mt-1 text-sm text-slate-600">资源可以在推流中新增并立即选中；只有点击“播放选中资源”才会更换混流器输入。</div>
          </div>
          <div className="flex flex-wrap gap-2" role="tablist" aria-label="直播资源面板">
            {([
              ["video", "视频库"],
              ["audio", "音频库"],
              ["source", "直播源"],
              ["playlist", "播放列表"],
            ] as const).map(([tab, label]) => (
              <Button key={tab} size="sm" tone={resourceTab === tab ? "primary" : "secondary"} onClick={() => setResourceTab(tab)}>{label}</Button>
            ))}
          </div>
        </div>

        {resourceTab === "video" ? (
          <div className="mt-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-slate-900">视频资源</div>
                <div className="mt-1 text-xs text-slate-500">选择后会出现在播放控制台；可在直播期间导入或上传。</div>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                <Button disabled={busy !== null} onClick={() => openTaskVideoPicker("completed")}>从成品导入</Button>
                <Button disabled={busy !== null} onClick={() => openTaskVideoPicker("raw")}>从原视频导入</Button>
                <label className="inline-flex cursor-pointer items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50">
                  {busy === "upload-video" ? "上传中..." : "批量上传视频"}
                  <input className="hidden" type="file" multiple accept="video/mp4,video/webm,video/quicktime,video/x-matroska" disabled={busy !== null} onChange={(event) => void uploadMedia("video", event)} />
                </label>
              </div>
            </div>
            <ManualCandidateList candidates={videoResourceCandidates} selected={selectedVideo} emptyText="直播媒体库中还没有视频。" onSelect={(candidate) => setSelectedVideo(candidate.item)} />
            <div className="mt-3 space-y-2">
              {dashboard?.library_media.filter((media) => media.media_type === "video").map((media) => (
                <div key={media.id} className="rounded-md border border-slate-100 px-2 py-2 text-xs text-slate-500">
                  {renamingVideoId === media.id ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <input value={videoRenameName} maxLength={180} disabled={busy !== null} onChange={(event) => setVideoRenameName(event.target.value)} className="min-w-48 flex-1 rounded border border-slate-300 px-2 py-1 text-xs text-slate-800" autoFocus />
                      <Button size="xs" tone="primary" disabled={busy !== null || !videoRenameName.trim()} onClick={() => void saveVideoRename(media)}>{busy === `video-rename-${media.id}` ? "保存中..." : "保存"}</Button>
                      <Button size="xs" disabled={busy !== null} onClick={() => { setRenamingVideoId(null); setVideoRenameName(""); }}>取消</Button>
                    </div>
                  ) : (
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate">{media.origin === "completed_video" ? "成品副本" : media.origin === "raw_video" ? "原视频副本" : "手动资源"}：{media.display_name}</span>
                      <span className="flex shrink-0 gap-2">
                        <Button size="xs" disabled={busy !== null} onClick={() => { setRenamingVideoId(media.id); setVideoRenameName(media.display_name); }}>改名</Button>
                        <Button size="xs" tone="danger" disabled={isActive || busy !== null} onClick={() => void deleteMedia(media)}>删除</Button>
                      </span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {resourceTab === "audio" ? (
          <div className="mt-5">
            <LiveAudioPlayer
              session={session}
              active={isLiveRunning}
              candidates={audioPlayerCandidates}
              selected={selectedAudio}
              playbackMode={audioPlaybackMode}
              volumePercent={audioVolumePercent}
              busy={busy !== null}
              onSelect={setSelectedAudio}
              onControl={(action, positionSeconds, setting) => void controlLiveAudio(action, positionSeconds, setting)}
              onPlayItem={(item) => void controlLiveAudio("play", undefined, undefined, item)}
              onPlaybackModeChange={(mode) => void controlLiveAudio("set_playback_mode", undefined, mode)}
            />
            {selectedAudioPlaylist ? (
              <div>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <Button size="xs" disabled={busy !== null} onClick={() => setSelectedAudioPlaylistId(null)}>← 返回我的歌单</Button>
                    <div className="mt-2 text-sm font-semibold text-slate-900">{selectedAudioPlaylist.display_name}</div>
                    <div className="mt-1 text-xs text-slate-500">上传歌曲会直接加入此歌单；选中歌曲后可作为当前直播的独立音频。</div>
                  </div>
                  <label className="inline-flex cursor-pointer items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50">
                    {busy === "upload-audio" ? "上传中..." : "批量上传歌曲"}
                    <input className="hidden" type="file" multiple accept="audio/mpeg,audio/mp4,audio/aac,audio/wav,audio/x-wav,audio/flac,audio/ogg,audio/opus,audio/webm" disabled={busy !== null} onChange={(event) => void uploadMedia("audio", event, selectedAudioPlaylist.id)} />
                  </label>
                </div>
                <div className="mt-3 space-y-2">
                  {selectedAudioPlaylistCandidates.map((candidate) => {
                    const media = dashboard?.library_media.find((item) => item.id === candidate.item.id);
                    return media ? (
                      <div key={media.id} className="flex items-center justify-between gap-3 text-xs text-slate-500">
                        <span className="truncate">歌曲：{media.display_name}</span>
                        <Button size="xs" tone="danger" disabled={isActive || busy !== null} onClick={() => void deleteMedia(media)}>删除歌曲</Button>
                      </div>
                    ) : null;
                  })}
                </div>
              </div>
            ) : (
              <div>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-slate-900">我的歌单</div>
                    <div className="mt-1 text-xs text-slate-500">像音乐播放器一样管理直播歌曲；删除歌单不会删除其中的歌曲文件。</div>
                  </div>
                  <div className="flex gap-2">
                    <input value={newAudioPlaylistName} maxLength={180} disabled={busy !== null} onChange={(event) => setNewAudioPlaylistName(event.target.value)} placeholder="新歌单名称" className="w-48 rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" onKeyDown={(event) => { if (event.key === "Enter") void createAudioPlaylist(); }} />
                    <Button tone="primary" disabled={busy !== null || !newAudioPlaylistName.trim()} onClick={() => void createAudioPlaylist()}>{busy === "audio-playlist-create" ? "创建中..." : "新建歌单"}</Button>
                  </div>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {(dashboard?.audio_playlists ?? []).map((playlist) => (
                    <div key={playlist.id} className="rounded-lg border border-slate-200 bg-gradient-to-br from-slate-50 to-white p-3 shadow-sm">
                      <button type="button" className="w-full text-left" onClick={() => setSelectedAudioPlaylistId(playlist.id)}>
                        <div className="truncate text-sm font-semibold text-slate-900">{playlist.display_name}</div>
                        <div className="mt-1 text-xs text-slate-500">{playlist.audio_media_ids.length} 首歌曲 · 点击进入</div>
                      </button>
                      <div className="mt-3 flex justify-end">
                        <Button size="xs" tone="danger" disabled={busy !== null} onClick={() => void deleteAudioPlaylist(playlist)}>{busy === `audio-playlist-delete-${playlist.id}` ? "删除中..." : "删除歌单"}</Button>
                      </div>
                    </div>
                  ))}
                </div>
                {(dashboard?.audio_playlists.length ?? 0) === 0 ? <EmptyState>还没有歌单。新建歌单后，进入其中上传直播歌曲。</EmptyState> : null}
                <div className="mt-6 border-t border-slate-100 pt-4">
                  <div className="text-sm font-medium text-slate-800">所有歌曲</div>
                  <div className="mt-1 text-xs text-slate-500">播放器上方显示全部歌曲；进入歌单后，播放队列会自动切换为该歌单。</div>
                </div>
              </div>
            )}
          </div>
        ) : null}

        {resourceTab === "source" ? (
          <div className="mt-5">
            <div className="text-sm font-semibold text-slate-900">直播源</div>
            <div className="mt-1 text-xs text-slate-500">添加 YouTube 直播页或公开 HTTP(S) 直播地址，选中后可作为视频输入推流。</div>
            <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]">
              <input value={liveSourceName} onChange={(event) => setLiveSourceName(event.target.value)} disabled={busy !== null} maxLength={180} placeholder="显示名称（可选）" className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" />
              <input value={liveSourceUrl} onChange={(event) => setLiveSourceUrl(event.target.value)} disabled={busy !== null} placeholder="https://www.youtube.com/watch?v=..." className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-100" />
              <div className="flex gap-2">
                <Button tone="primary" disabled={busy !== null || !liveSourceUrl.trim()} onClick={() => void saveLiveSource()}>{busy?.startsWith("source-") ? "保存中..." : editingLiveSourceId ? "更新" : "添加"}</Button>
                {editingLiveSourceId ? <Button disabled={busy !== null} onClick={resetLiveSourceForm}>取消</Button> : null}
              </div>
            </div>
            <ManualCandidateList candidates={liveSourceCandidates} selected={selectedVideo} emptyText="还没有直播源。" onSelect={(candidate) => setSelectedVideo(candidate.item)} />
            <div className="mt-3 space-y-2">
              {dashboard?.live_sources.map((source) => (
                <div key={source.id} className="flex items-center justify-between gap-3 text-xs text-slate-500">
                  <span className="truncate">{source.display_name}</span>
                  <span className="flex shrink-0 gap-2">
                    <Button size="xs" disabled={isActive || busy !== null} onClick={() => editLiveSource(source)}>编辑</Button>
                    <Button size="xs" tone="danger" disabled={isActive || busy !== null} onClick={() => void deleteLiveSource(source)}>删除</Button>
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {resourceTab === "playlist" ? (
          <div className="mt-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-slate-900">自动播放列表</div>
                <div className="mt-1 text-xs text-slate-500">用于“开始直播”。直播中请使用上方播放器手动切换资源。</div>
              </div>
              <Button tone="primary" disabled={isActive || busy !== null} onClick={() => void savePlaylist()}>{busy === "playlist" ? "保存中..." : "保存播放列表"}</Button>
            </div>
            <div className="mt-4 grid gap-4 lg:grid-cols-2">
              <div>
                <div className="text-sm font-medium text-slate-800">视频与直播源</div>
                <CandidateList candidates={[...videoResourceCandidates, ...liveSourceCandidates]} selected={videos} disabled={isActive || busy !== null} emptyText="尚未添加视频资源。" onToggle={(candidate, checked) => toggleItem(setVideos, videos, candidate, checked)} />
                <SelectionList title="视频播放顺序" items={videos} candidates={videoCandidateMap} onMove={(index, direction) => setVideos(moveItem(videos, index, direction))} onRemove={(index) => setVideos(videos.filter((_, itemIndex) => itemIndex !== index))} />
              </div>
              <div>
                <div className="text-sm font-medium text-slate-800">背景音频</div>
                <CandidateList candidates={audioCandidates} selected={audios} disabled={isActive || busy !== null} emptyText="尚未选择独立音频。" onToggle={(candidate, checked) => toggleItem(setAudios, audios, candidate, checked)} />
                <SelectionList title="音频播放顺序" items={audios} candidates={audioCandidateMap} onMove={(index, direction) => setAudios(moveItem(audios, index, direction))} onRemove={(index) => setAudios(audios.filter((_, itemIndex) => itemIndex !== index))} />
              </div>
            </div>
          </div>
        ) : null}
      </Section>
    </div>
    {taskVideoPicker ? (
      <TaskVideoPicker
        title={taskVideoPicker === "completed" ? "从最近任务选择成品视频" : "从最近任务选择原视频"}
        candidates={taskVideoPicker === "completed" ? completedVideoCandidates : rawVideoCandidates}
        selected={taskVideoSelection}
        onToggle={(candidate, checked) => toggleItem(setTaskVideoSelection, taskVideoSelection, candidate, checked)}
        onApply={() => void applyTaskVideoSelection()}
        applying={busy === "import-video"}
        onClose={() => {
          setTaskVideoPicker(null);
          setTaskVideoSelection([]);
        }}
      />
    ) : null}
    </>
  );
}
