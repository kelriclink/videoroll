import { useEffect, useRef } from "react";
import AudioPlayer, { useAudioPlayer, useAudioPlayerElement } from "react-modern-audio-player";
import { liveApi } from "../../../api/live";
import type { LiveAudioControlAction, LiveSession, MediaCandidate, PlaybackMode, PlaylistItem } from "../types";
import { itemKey } from "../utils";

type LivePlayerTrack = {
  id: number;
  item: PlaylistItem;
  name: string;
  writer: string;
  src: string;
};

function LiveAudioPlayerBridge({
  tracks,
  hasRemoteTrack,
  onSelect,
  onPlayItem,
  onControl,
}: {
  tracks: LivePlayerTrack[];
  hasRemoteTrack: boolean;
  onSelect: (item: PlaylistItem) => void;
  onPlayItem: (item: PlaylistItem) => void;
  onControl: (action: LiveAudioControlAction, positionSeconds?: number, setting?: PlaybackMode | number) => void;
}) {
  const { currentTrack, isPlaying } = useAudioPlayer();
  const { audioEl } = useAudioPlayerElement();
  const audioElementRef = useRef<HTMLAudioElement | null>(null);
  const readyRef = useRef(false);
  const previousTrackIdRef = useRef<number | null>(null);
  const previousPlayingRef = useRef<boolean | null>(null);
  const suppressUntilRef = useRef(0);
  const volumeTimerRef = useRef<number | null>(null);
  const track = tracks.find((item) => item.id === currentTrack?.id) ?? null;

  useEffect(() => {
    const timer = window.setTimeout(() => { readyRef.current = true; }, 700);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const trackId = track?.id ?? null;
    if (previousTrackIdRef.current === null) {
      previousTrackIdRef.current = trackId;
      return;
    }
    if (trackId === previousTrackIdRef.current || !track) return;
    previousTrackIdRef.current = trackId;
    suppressUntilRef.current = Date.now() + 900;
    onSelect(track.item);
    onPlayItem(track.item);
  }, [onPlayItem, onSelect, track]);

  useEffect(() => {
    if (previousPlayingRef.current === null) {
      previousPlayingRef.current = isPlaying;
      return;
    }
    if (previousPlayingRef.current === isPlaying) return;
    previousPlayingRef.current = isPlaying;
    if (!readyRef.current || Date.now() < suppressUntilRef.current || !track) return;
    if (isPlaying) {
      if (hasRemoteTrack) onControl("resume");
      else onPlayItem(track.item);
    } else if (hasRemoteTrack) {
      onControl("pause");
    }
  }, [hasRemoteTrack, isPlaying, onControl, onPlayItem, track]);

  useEffect(() => {
    if (!audioEl) return;
    audioElementRef.current = audioEl;
    const keepBrowserMuted = () => {
      const element = audioElementRef.current;
      if (element && !element.muted) element.muted = true;
    };
    const sendSeek = () => {
      if (readyRef.current && Date.now() >= suppressUntilRef.current && !audioEl.ended) {
        onControl("seek", audioEl.currentTime);
      }
    };
    const sendVolume = () => {
      keepBrowserMuted();
      if (!readyRef.current || Date.now() < suppressUntilRef.current) return;
      if (volumeTimerRef.current !== null) window.clearTimeout(volumeTimerRef.current);
      volumeTimerRef.current = window.setTimeout(() => {
        onControl("set_volume", undefined, Math.round(audioEl.volume * 100));
      }, 180);
    };
    keepBrowserMuted();
    audioEl.addEventListener("seeked", sendSeek);
    audioEl.addEventListener("volumechange", sendVolume);
    return () => {
      audioEl.removeEventListener("seeked", sendSeek);
      audioEl.removeEventListener("volumechange", sendVolume);
      audioElementRef.current = null;
      if (volumeTimerRef.current !== null) window.clearTimeout(volumeTimerRef.current);
    };
  }, [audioEl, onControl]);

  return null;
}

export function LiveAudioPlayer({
  session,
  active,
  candidates,
  selected,
  playbackMode,
  volumePercent,
  busy,
  onSelect,
  onPlayItem,
  onControl,
  onPlaybackModeChange,
}: {
  session?: LiveSession;
  active: boolean;
  candidates: MediaCandidate[];
  selected: PlaylistItem | null;
  playbackMode: PlaybackMode;
  volumePercent: number;
  busy: boolean;
  onSelect: (item: PlaylistItem) => void;
  onPlayItem: (item: PlaylistItem) => void;
  onControl: (action: LiveAudioControlAction, positionSeconds?: number, setting?: PlaybackMode | number) => void;
  onPlaybackModeChange: (mode: PlaybackMode) => void;
}) {
  const tracks: LivePlayerTrack[] = candidates.map((candidate, index) => ({
    id: index + 1,
    item: candidate.item,
    name: candidate.displayName,
    writer: candidate.subtitle,
    src: liveApi.mediaStreamUrl(candidate.item.id),
  }));
  const currentKey = session?.current_audio ? itemKey(session.current_audio) : null;
  const selectedKey = selected ? itemKey(selected) : null;
  const currentTrack = tracks.find((track) => itemKey(track.item) === currentKey)
    ?? tracks.find((track) => itemKey(track.item) === selectedKey)
    ?? tracks[0];
  const playerKey = `${currentKey ?? selectedKey ?? "empty"}:${session?.audio_player_status ?? "idle"}`;
  const canControl = active && !busy;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-800 bg-[#10151d] p-4 text-slate-100 shadow-[0_20px_45px_-28px_rgba(15,23,42,0.95)]">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">混流器音乐播放器</div>
          <div className="mt-1 text-xs text-slate-400">
            播放队列、进度、上一首/下一首和音量均同步到直播混流器；浏览器内音频保持静音，避免与直播预览叠声。
          </div>
        </div>
        <div className="inline-flex rounded-lg border border-slate-700 bg-slate-900 p-1 text-xs">
          {(["sequential", "shuffle"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              disabled={!canControl}
              onClick={() => onPlaybackModeChange(mode)}
              className={`rounded-md px-3 py-1.5 transition disabled:cursor-not-allowed disabled:opacity-50 ${playbackMode === mode ? "bg-sky-400 text-slate-950 shadow-sm" : "text-slate-300 hover:bg-slate-800"}`}
            >
              {mode === "sequential" ? "顺序播放" : "随机播放"}
            </button>
          ))}
        </div>
      </div>

      {tracks.length === 0 ? <div className="rounded-lg border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-400">当前队列还没有歌曲。</div> : (
        <AudioPlayer
          key={playerKey}
          playList={tracks}
          colorScheme="dark"
          rootContainerProps={{ className: "live-mixer-audio-player" }}
          audioInitialState={{
            curPlayId: currentTrack?.id ?? 0,
            currentTime: session?.audio_position_seconds ?? 0,
            isPlaying: session?.audio_player_status === "playing",
            volume: Math.min(1, Math.max(0, volumePercent / 100)),
            preload: "metadata",
            repeatType: "ONE",
          }}
          activeUI={{
            all: true,
            playList: "unSortable",
            prevNnext: false,
            repeatType: false,
            playbackRate: false,
            progress: "bar",
          }}
        >
          <LiveAudioPlayerBridge
            tracks={tracks}
            hasRemoteTrack={Boolean(session?.current_audio)}
            onSelect={onSelect}
            onPlayItem={onPlayItem}
            onControl={onControl}
          />
        </AudioPlayer>
      )}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/45 px-3 py-2 text-xs text-slate-300">
        <span>{session?.current_audio ? (session.mix_audio ? "当前为混音模式" : "当前独立音频会替换视频原声") : "当前使用视频原声"}</span>
        <span className="flex flex-wrap gap-2">
          <button type="button" disabled={!canControl || tracks.length === 0} onClick={() => onControl("previous")} className="rounded-md border border-slate-600 px-3 py-1.5 font-medium hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-45">上一首</button>
          <button type="button" disabled={!canControl || tracks.length === 0} onClick={() => onControl("next")} className="rounded-md border border-slate-600 px-3 py-1.5 font-medium hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-45">下一首</button>
          <button type="button" disabled={!canControl} onClick={() => onControl("original")} className="rounded-md border border-slate-600 px-3 py-1.5 font-medium hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-45">立即切回视频原声</button>
        </span>
      </div>
    </div>
  );
}
