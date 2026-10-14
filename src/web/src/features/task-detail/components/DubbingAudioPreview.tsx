import { useEffect, useState } from "react";

/**
 * On-demand audio preview with explicit cookie-bearing fetch and real errors.
 * Native <audio src> only shows 0:00 when the backend returns 401/404; it
 * cannot surface the backend response. Fetch only when a user requests it,
 * avoiding dozens of full WAV downloads on each 30-item editor page.
 */
export function DubbingAudioPreview({ src }: { src: string }) {
  const [requested, setRequested] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!requested) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setError(null);
    setAudioUrl(null);
    void fetch(src, { credentials: "include", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`音频请求失败：HTTP ${response.status} ${response.statusText}`);
        }
        const file = await response.blob();
        if (file.size < 44) throw new Error("音频文件为空或未完整保存");
        return file;
      })
      .then((file) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(file);
        setAudioUrl(objectUrl);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [requested, src]);

  if (error) {
    return <div className="space-y-1 text-xs text-red-700">
      {error}
      <button type="button" className="ml-2 rounded border px-2 py-1"
        onClick={() => { setRequested(false); setAudioUrl(null); setError(null); }}>
        重试
      </button>
    </div>;
  }

  if (!requested) {
    return <button type="button" className="rounded border px-2 py-1 text-xs"
      onClick={() => setRequested(true)}>加载音频试听</button>;
  }

  if (!audioUrl) return <span className="text-xs text-slate-500">正在加载音频…</span>;
  return <audio controls preload="metadata" src={audioUrl} />;
}
