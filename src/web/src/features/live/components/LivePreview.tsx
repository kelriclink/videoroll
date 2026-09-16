import Hls from "hls.js";
import { useCallback, useEffect, useRef, useState } from "react";
import { liveApi } from "../../../api/live";

export function LivePreview({ active }: { active: boolean }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [message, setMessage] = useState(active ? "正在连接预览流…" : "开始推流后，这里会显示当前输出画面。");
  const [retryAttempt, setRetryAttempt] = useState(0);
  const retryTimerRef = useRef<number | null>(null);
  const previewUrl = liveApi.previewUrl();

  const retryPreview = useCallback(() => {
    if (retryTimerRef.current !== null) return;
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      setRetryAttempt((current) => current + 1);
    }, 1500);
  }, []);

  useEffect(() => () => {
    if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (!active) {
      if (retryTimerRef.current !== null) {
        window.clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      video.pause();
      video.removeAttribute("src");
      video.load();
      setMessage("开始推流后，这里会显示当前输出画面。");
      return;
    }
    setMessage("正在连接预览流…");
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = previewUrl;
      void video.play().catch(() => undefined);
      return () => {
        video.pause();
        video.removeAttribute("src");
        video.load();
      };
    }
    if (!Hls.isSupported()) {
      setMessage("当前浏览器不支持直播预览。");
      return;
    }
    const hls = new Hls({
      enableWorker: true,
      lowLatencyMode: true,
      liveSyncDurationCount: 3,
      manifestLoadingMaxRetry: 8,
      manifestLoadingRetryDelay: 1000,
    });
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      setMessage("");
      void video.play().catch(() => undefined);
    });
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        setMessage("预览流暂不可用，正在等待混流器输出…");
        retryPreview();
      }
    });
    hls.loadSource(previewUrl);
    hls.attachMedia(video);
    return () => hls.destroy();
  }, [active, previewUrl, retryAttempt, retryPreview]);

  return (
    <div className="overflow-hidden rounded-lg border border-slate-700 bg-slate-950 shadow-inner">
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2 text-xs text-slate-300">
        <span className="font-medium">当前推流预览</span>
        <span className={active ? "text-emerald-300" : "text-slate-500"}>{active ? "LIVE · HLS" : "OFFLINE"}</span>
      </div>
      <div className="relative aspect-video bg-black">
        <video
          ref={videoRef}
          controls
          muted
          playsInline
          className="h-full w-full object-contain"
          onCanPlay={() => {
            if (active) setMessage("");
          }}
          onPlaying={() => setMessage("")}
          onError={() => {
            if (active) {
              setMessage("预览流暂不可用，正在等待混流器输出…");
              retryPreview();
            }
          }}
        />
        {message ? <div className="absolute inset-0 flex items-center justify-center bg-slate-950/70 px-6 text-center text-sm text-slate-300">{message}</div> : null}
      </div>
      <div className="border-t border-slate-800 px-3 py-2 text-[11px] text-slate-400">预览约有数秒延迟；静音自动播放，取消静音可监听当前混流结果。</div>
    </div>
  );
}
