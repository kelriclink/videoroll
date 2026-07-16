import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useConfirm, useToast } from "../components/feedbackContext";
import { Button, EmptyState, PageHeader, Section } from "../components/ui";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type LiveSettings = {
  rtmp_url: string;
  stream_key_set: boolean;
  video_bitrate_kbps: number;
  audio_bitrate_kbps: number;
  fps: number;
  keyframe_interval_seconds: number;
};

type PlaylistItem = { source: "library" | "task_asset"; id: string };
type PlaybackMode = "sequential" | "shuffle";

type LivePlaylist = {
  video_items: PlaylistItem[];
  audio_items: PlaylistItem[];
  playback_mode: PlaybackMode;
  loop_playlist: boolean;
};

type LiveMedia = {
  id: string;
  media_type: "video" | "audio";
  origin: "upload" | "completed_video";
  source_task_id?: string | null;
  source_asset_id?: string | null;
  display_name: string;
  storage_key: string;
  content_type: string;
  size_bytes: number;
  sha256?: string | null;
  created_at?: string | null;
};

type CompletedVideo = {
  id: string;
  task_id: string;
  display_name: string;
  storage_key: string;
  size_bytes?: number | null;
  duration_ms?: number | null;
  created_at: string;
};

type CurrentMedia = PlaylistItem & { display_name: string };
type LiveSession = {
  status: "idle" | "starting" | "running" | "paused" | "stopped" | "failed";
  started_at?: string | null;
  updated_at?: string | null;
  stopped_at?: string | null;
  current_video?: CurrentMedia | null;
  current_audio?: CurrentMedia | null;
  last_error?: string | null;
};

type LiveDashboard = {
  settings: LiveSettings;
  session: LiveSession;
  playlist: LivePlaylist;
  library_media: LiveMedia[];
  completed_videos: CompletedVideo[];
};

type MediaCandidate = {
  item: PlaylistItem;
  displayName: string;
  subtitle: string;
  sizeBytes?: number | null;
};

function itemKey(item: PlaylistItem): string {
  return `${item.source}:${item.id}`;
}

function formatBytes(value?: number | null): string {
  if (!value || value <= 0) return "大小未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

function sessionLabel(status: LiveSession["status"]): string {
  return {
    idle: "未开始",
    starting: "启动中",
    running: "推流中",
    paused: "已暂停",
    stopped: "已停止",
    failed: "异常停止",
  }[status];
}

function sessionClass(status: LiveSession["status"]): string {
  if (status === "running") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "paused" || status === "starting") return "border-amber-200 bg-amber-50 text-amber-900";
  if (status === "failed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function moveItem(items: PlaylistItem[], index: number, direction: -1 | 1): PlaylistItem[] {
  const nextIndex = index + direction;
  if (nextIndex < 0 || nextIndex >= items.length) return items;
  const next = [...items];
  [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
  return next;
}

function SelectionList({
  title,
  items,
  candidates,
  onMove,
  onRemove,
}: {
  title: string;
  items: PlaylistItem[];
  candidates: Map<string, MediaCandidate>;
  onMove: (index: number, direction: -1 | 1) => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-slate-900">{title}</div>
        <div className="text-xs text-slate-500">{items.length} 项</div>
      </div>
      {items.length === 0 ? (
        <div className="mt-2 text-xs text-slate-500">尚未选择资源。</div>
      ) : (
        <ol className="mt-2 space-y-2">
          {items.map((item, index) => {
            const candidate = candidates.get(itemKey(item));
            return (
              <li key={itemKey(item)} className="flex items-center gap-2 rounded border border-slate-200 bg-white px-2 py-2">
                <span className="w-5 text-center text-xs font-semibold text-slate-500">{index + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-slate-800">{candidate?.displayName ?? "已删除的资源"}</div>
                  <div className="truncate text-[11px] text-slate-500">{candidate?.subtitle ?? item.id}</div>
                </div>
                <div className="flex gap-1">
                  <Button size="xs" disabled={index === 0} onClick={() => onMove(index, -1)}>↑</Button>
                  <Button size="xs" disabled={index === items.length - 1} onClick={() => onMove(index, 1)}>↓</Button>
                  <Button size="xs" tone="danger" onClick={() => onRemove(index)}>移除</Button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function CandidateList({
  candidates,
  selected,
  onToggle,
  disabled,
  emptyText,
}: {
  candidates: MediaCandidate[];
  selected: PlaylistItem[];
  onToggle: (candidate: MediaCandidate, checked: boolean) => void;
  disabled: boolean;
  emptyText: string;
}) {
  const selectedKeys = new Set(selected.map(itemKey));
  if (candidates.length === 0) return <EmptyState>{emptyText}</EmptyState>;
  return (
    <div className="mt-3 max-h-72 space-y-2 overflow-auto pr-1">
      {candidates.map((candidate) => {
        const checked = selectedKeys.has(itemKey(candidate.item));
        return (
          <label key={itemKey(candidate.item)} className="flex cursor-pointer items-start gap-3 rounded-md border border-slate-200 bg-white p-3 hover:border-slate-300">
            <input
              type="checkbox"
              className="mt-1"
              checked={checked}
              disabled={disabled}
              onChange={(event) => onToggle(candidate, event.target.checked)}
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-slate-900">{candidate.displayName}</span>
              <span className="mt-0.5 block truncate text-xs text-slate-500">{candidate.subtitle}</span>
              <span className="mt-1 block text-[11px] text-slate-400">{formatBytes(candidate.sizeBytes)}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

export default function LivePage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [dashboard, setDashboard] = useState<LiveDashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [rtmpUrl, setRtmpUrl] = useState("");
  const [streamKey, setStreamKey] = useState("");
  const [videoBitrate, setVideoBitrate] = useState(4500);
  const [audioBitrate, setAudioBitrate] = useState(160);
  const [fps, setFps] = useState(30);
  const [keyframeSeconds, setKeyframeSeconds] = useState(2);
  const [videos, setVideos] = useState<PlaylistItem[]>([]);
  const [audios, setAudios] = useState<PlaylistItem[]>([]);
  const [playbackMode, setPlaybackMode] = useState<PlaybackMode>("sequential");
  const [loopPlaylist, setLoopPlaylist] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchJson<LiveDashboard>(`${ORCHESTRATOR_URL}/live`);
      setDashboard(next);
      setRtmpUrl(next.settings.rtmp_url);
      setVideoBitrate(next.settings.video_bitrate_kbps);
      setAudioBitrate(next.settings.audio_bitrate_kbps);
      setFps(next.settings.fps);
      setKeyframeSeconds(next.settings.keyframe_interval_seconds);
      setVideos(next.playlist.video_items);
      setAudios(next.playlist.audio_items);
      setPlaybackMode(next.playlist.playback_mode);
      setLoopPlaylist(next.playlist.loop_playlist);
      setError(null);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!dashboard || !["starting", "running", "paused"].includes(dashboard.session.status)) return;
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [dashboard, refresh]);

  const videoCandidates = useMemo<MediaCandidate[]>(() => {
    if (!dashboard) return [];
    return [
      ...dashboard.library_media
        .filter((media) => media.media_type === "video")
        .map((media) => ({
          item: { source: "library" as const, id: media.id },
          displayName: media.display_name,
          subtitle: media.origin === "completed_video"
            ? `已复制到直播媒体库 · 来源任务 ${media.source_task_id?.slice(0, 8) || "未知"}`
            : `手动上传 · ${media.storage_key}`,
          sizeBytes: media.size_bytes,
        })),
      ...dashboard.completed_videos.map((video) => ({
        item: { source: "task_asset" as const, id: video.id },
        displayName: video.display_name,
        subtitle: `已完成视频 · 任务 ${video.task_id.slice(0, 8)}`,
        sizeBytes: video.size_bytes,
      })),
    ];
  }, [dashboard]);

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

  const videoCandidateMap = useMemo(() => new Map(videoCandidates.map((candidate) => [itemKey(candidate.item), candidate])), [videoCandidates]);
  const audioCandidateMap = useMemo(() => new Map(audioCandidates.map((candidate) => [itemKey(candidate.item), candidate])), [audioCandidates]);
  const isActive = Boolean(dashboard && ["starting", "running", "paused"].includes(dashboard.session.status));

  function toggleItem(setter: (items: PlaylistItem[]) => void, items: PlaylistItem[], candidate: MediaCandidate, checked: boolean) {
    if (checked) setter([...items, candidate.item]);
    else setter(items.filter((item) => itemKey(item) !== itemKey(candidate.item)));
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
      const settings = await fetchJson<LiveSettings>(`${ORCHESTRATOR_URL}/live/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
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
      const playlist = await fetchJson<LivePlaylist>(`${ORCHESTRATOR_URL}/live/playlist`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_items: videos, audio_items: audios, playback_mode: playbackMode, loop_playlist: loopPlaylist }),
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

  async function uploadMedia(mediaType: "video" | "audio", event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(`upload-${mediaType}`);
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      await fetchJson<LiveMedia>(`${ORCHESTRATOR_URL}/live/media/${mediaType}`, { method: "POST", body: form });
      toast({ kind: "success", title: `${mediaType === "video" ? "视频" : "音频"}已加入直播资源库`, message: file.name });
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
      await fetchJson(`${ORCHESTRATOR_URL}/live/media/${media.id}`, { method: "DELETE" });
      toast({ kind: "success", title: "直播媒体已删除" });
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
      const session = await fetchJson<LiveSession>(`${ORCHESTRATOR_URL}/live/actions/${action}`, { method: "POST" });
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
  const session = dashboard?.session;

  return (
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
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-base font-semibold text-slate-950">播放策略</div>
            <div className="mt-1 text-sm text-slate-600">视频为必选资源；音频可选，未选择独立音频时保留视频原声。</div>
          </div>
          <Button tone="primary" disabled={isActive || busy !== null} onClick={() => void savePlaylist()}>{busy === "playlist" ? "保存中..." : "保存播放列表"}</Button>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
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
        </div>
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-base font-semibold text-slate-950">视频资源</div>
              <div className="mt-1 text-sm text-slate-600">选择手动上传的视频；已完成视频会在保存播放列表时复制到直播媒体库。</div>
            </div>
            <label className="inline-flex cursor-pointer items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50">
              {busy === "upload-video" ? "上传中..." : "上传视频"}
              <input className="hidden" type="file" accept="video/mp4,video/webm,video/quicktime,video/x-matroska" disabled={isActive || busy !== null} onChange={(event) => void uploadMedia("video", event)} />
            </label>
          </div>
          <CandidateList candidates={videoCandidates} selected={videos} disabled={isActive || busy !== null} emptyText="还没有可用视频。上传视频或先完成一个任务。" onToggle={(candidate, checked) => toggleItem(setVideos, videos, candidate, checked)} />
          <SelectionList title="视频播放顺序" items={videos} candidates={videoCandidateMap} onMove={(index, direction) => setVideos(moveItem(videos, index, direction))} onRemove={(index) => setVideos(videos.filter((_, itemIndex) => itemIndex !== index))} />
          <div className="mt-3 space-y-2">
            {dashboard?.library_media.filter((media) => media.media_type === "video").map((media) => (
              <div key={media.id} className="flex items-center justify-between gap-3 text-xs text-slate-500">
                <span className="truncate">{media.origin === "completed_video" ? "成品副本" : "手动资源"}：{media.display_name}</span>
                <Button size="xs" tone="danger" disabled={isActive || busy !== null} onClick={() => void deleteMedia(media)}>删除</Button>
              </div>
            ))}
          </div>
        </Section>

        <Section>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-base font-semibold text-slate-950">音频资源</div>
              <div className="mt-1 text-sm text-slate-600">上传背景音频；每个视频会对应使用当前顺序的音频，音频会自动循环至视频结束。</div>
            </div>
            <label className="inline-flex cursor-pointer items-center justify-center rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50">
              {busy === "upload-audio" ? "上传中..." : "上传音频"}
              <input className="hidden" type="file" accept="audio/mpeg,audio/mp4,audio/aac,audio/wav,audio/x-wav,audio/flac,audio/ogg,audio/opus,audio/webm" disabled={isActive || busy !== null} onChange={(event) => void uploadMedia("audio", event)} />
            </label>
          </div>
          <CandidateList candidates={audioCandidates} selected={audios} disabled={isActive || busy !== null} emptyText="尚未上传独立音频；留空时将使用视频原声。" onToggle={(candidate, checked) => toggleItem(setAudios, audios, candidate, checked)} />
          <SelectionList title="音频播放顺序" items={audios} candidates={audioCandidateMap} onMove={(index, direction) => setAudios(moveItem(audios, index, direction))} onRemove={(index) => setAudios(audios.filter((_, itemIndex) => itemIndex !== index))} />
          <div className="mt-3 space-y-2">
            {dashboard?.library_media.filter((media) => media.media_type === "audio").map((media) => (
              <div key={media.id} className="flex items-center justify-between gap-3 text-xs text-slate-500">
                <span className="truncate">音频资源：{media.display_name}</span>
                <Button size="xs" tone="danger" disabled={isActive || busy !== null} onClick={() => void deleteMedia(media)}>删除</Button>
              </div>
            ))}
          </div>
        </Section>
      </div>
    </div>
  );
}
