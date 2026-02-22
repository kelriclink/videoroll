import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { fetchJson } from "../lib/http";
import { BILIBILI_PUBLISHER_URL, ORCHESTRATOR_URL, SUBTITLE_SERVICE_URL } from "../lib/urls";
import { Asset, PublishJob, SubtitleJob, Task } from "../lib/types";

type SubtitleActionResponse = { job_id: string; status: string };
type PublishResponse = { state: string; aid?: string | null; bvid?: string | null; response?: any };
type PublishSettings = { default_meta: any };
type YouTubeMeta = {
  title: string;
  description: string;
  webpage_url: string;
  uploader?: string | null;
  upload_date?: string | null;
  duration?: number | null;
};
type YouTubeMetaActionResponse = { metadata: YouTubeMeta };
type YouTubeDownloadActionResponse = { metadata: YouTubeMeta; video_asset: Asset; metadata_asset: Asset; cover_asset?: Asset | null };
type TranslateTestResponse = { translated_text: string };
type SubtitleAutoProfile = {
  formats: string[];
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  asr_engine: string;
  asr_language: string;
  asr_model?: string | null;
  translate_enabled: boolean;
  translate_provider: string;
  target_lang: string;
  translate_style: string;
  translate_enable_summary: boolean;
  bilingual: boolean;
  auto_publish: boolean;
  publish_title_prefix: string;
  publish_translate_title: boolean;
  publish_use_youtube_cover: boolean;
};

export default function TaskDetailPage() {
  const { taskId } = useParams();
  const [task, setTask] = useState<Task | null>(null);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [subtitleJobs, setSubtitleJobs] = useState<SubtitleJob[] | null>(null);
  const [publishJobs, setPublishJobs] = useState<PublishJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const [subtitleFormats, setSubtitleFormats] = useState<{ srt: boolean; ass: boolean }>({ srt: true, ass: false });
  const [burnIn, setBurnIn] = useState(false);
  const [softSub, setSoftSub] = useState(false);
  const [asrEngine, setAsrEngine] = useState("auto");
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrModel, setAsrModel] = useState<string>("");
  const [whisperModels, setWhisperModels] = useState<Array<{ name: string; path: string }> | null>(null);
  const [translateEnabled, setTranslateEnabled] = useState(false);
  const [bilingual, setBilingual] = useState(false);
  const [targetLang, setTargetLang] = useState("zh");
  const [translateProvider, setTranslateProvider] = useState("mock");
  const [translateStyle, setTranslateStyle] = useState("口语自然");
  const [translateEnableSummary, setTranslateEnableSummary] = useState(true);
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);

  const [publishSettings, setPublishSettings] = useState<PublishSettings | null>(null);
  const [publishMetaText, setPublishMetaText] = useState<string>("{}");
  const [publishVideoKey, setPublishVideoKey] = useState<string>("");
  const [publishCoverKey, setPublishCoverKey] = useState<string>("");
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [youtubeMeta, setYoutubeMeta] = useState<YouTubeMeta | null>(null);
  const [didAutoFillPublishMeta, setDidAutoFillPublishMeta] = useState(false);
  const [didAutoPickCover, setDidAutoPickCover] = useState(false);

  const refresh = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!taskId) return;
      if (!opts?.silent) setError(null);
      try {
        const [t, a, sj, pj] = await Promise.all([
          fetchJson<Task>(`${ORCHESTRATOR_URL}/tasks/${taskId}`),
          fetchJson<Asset[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/assets`),
          fetchJson<SubtitleJob[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/subtitle_jobs`),
          fetchJson<PublishJob[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_jobs`),
        ]);
        setTask(t);
        setAssets(a);
        setSubtitleJobs(sj);
        setPublishJobs(pj);
      } catch (e: unknown) {
        if (!opts?.silent) setError(e instanceof Error ? e.message : String(e));
      }
    },
    [taskId],
  );

  useEffect(() => {
    refresh();
  }, [taskId]);

  useEffect(() => {
    if (!taskId) return;
    setPublishVideoKey("");
    setPublishCoverKey("");
    setCoverFile(null);
    setYoutubeMeta(null);
    setDidAutoFillPublishMeta(false);
    setDidAutoPickCover(false);
    fetchJson<PublishSettings>(`${BILIBILI_PUBLISHER_URL}/bilibili/publish/settings`)
      .then((s) => {
        setPublishSettings(s);
        setPublishMetaText(JSON.stringify(s.default_meta ?? {}, null, 2));
      })
      .catch(() => {
        setPublishSettings(null);
      });
  }, [taskId]);

  useEffect(() => {
    fetchJson<Array<{ name: string; path: string }>>(`${SUBTITLE_SERVICE_URL}/subtitle/models`)
      .then((m) => setWhisperModels(m))
      .catch(() => setWhisperModels(null));
  }, []);

  useEffect(() => {
    if (!taskId) return;
    (async () => {
      try {
        const [profile, translateSettings] = await Promise.all([
          fetchJson<SubtitleAutoProfile>(`${SUBTITLE_SERVICE_URL}/subtitle/auto/profile`),
          fetchJson<{ openai_api_key_set: boolean }>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`),
        ]);

        const formats = Array.isArray(profile.formats) ? profile.formats : [];
        setSubtitleFormats({
          srt: formats.includes("srt"),
          ass: formats.includes("ass"),
        });
        setBurnIn(Boolean(profile.burn_in));
        setSoftSub(Boolean(profile.soft_sub));
        setAsrEngine(profile.asr_engine || "auto");
        setAsrLanguage(profile.asr_language || "auto");
        setAsrModel((profile.asr_model ?? "").trim());
        setTranslateEnabled(Boolean(profile.translate_enabled));
        setBilingual(Boolean(profile.bilingual));
        setTargetLang(profile.target_lang || "zh");
        setTranslateProvider(profile.translate_provider || "openai");
        setTranslateStyle(profile.translate_style || "口语自然");
        setTranslateEnableSummary(Boolean(profile.translate_enable_summary));
        setOpenaiKeySet(Boolean(translateSettings.openai_api_key_set));
      } catch (e) {
        // Fallback: only fetch OpenAI key status so UI can show guidance.
        try {
          const s = await fetchJson<{ openai_api_key_set: boolean }>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`);
          setOpenaiKeySet(Boolean(s.openai_api_key_set));
        } catch {}
      }
    })();
  }, [taskId]);

  function clampText(text: string, maxLen: number): string {
    const s = (text ?? "").trim();
    if (s.length <= maxLen) return s;
    if (maxLen <= 1) return s.slice(0, maxLen);
    return s.slice(0, maxLen - 1) + "…";
  }

  function hasCjk(text: string): boolean {
    return /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uF900-\uFAFF]/.test(text ?? "");
  }

  function ensureShurouPrefix(title: string): string {
    const t = (title ?? "").trim();
    if (!t) return t;
    return t.startsWith("【熟肉】") ? t : `【熟肉】${t}`;
  }

  async function translateTitleToZh(title: string): Promise<string> {
    const t = (title ?? "").trim();
    if (!t) return t;
    if (hasCjk(t)) return t;
    if (openaiKeySet === false) return t;
    try {
      const resp = await fetchJson<TranslateTestResponse>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: t, target_lang: "zh", style: translateStyle || "口语自然" }),
      });
      return (resp.translated_text ?? "").trim() || t;
    } catch {
      return t;
    }
  }

  function buildBilibiliDesc(youtubeDesc: string, sourceUrl: string): string {
    const src = (sourceUrl ?? "").trim();
    const tail = src ? `\n\n原视频：${src}` : "";
    const maxLen = 2000;
    if (!tail) return clampText((youtubeDesc ?? "").trim(), maxLen);
    if (tail.length >= maxLen) return clampText(tail, maxLen);

    let base = (youtubeDesc ?? "").trim();
    const avail = maxLen - tail.length;
    if (base.length > avail) base = clampText(base, avail);
    const out = (base ? base + tail : `原视频：${src}`).trim();
    return out.length > maxLen ? clampText(out, maxLen) : out;
  }

  const fetchYouTubeMeta = useCallback(async () => {
    if (!taskId) return null;
    const resp = await fetchJson<YouTubeMetaActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_meta`, {
      method: "POST",
    });
    setYoutubeMeta(resp.metadata);
    return resp.metadata;
  }, [taskId]);

  const applyYouTubeMetaToPublishMeta = useCallback(
    async (yt: YouTubeMeta) => {
      const sourceUrl = (task?.source_url ?? "").trim();
      if (!sourceUrl) throw new Error("task.source_url is empty");

      let metaIn: any;
      try {
        metaIn = JSON.parse(publishMetaText);
      } catch {
        throw new Error("publish meta is not valid JSON");
      }
      if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) throw new Error("publish meta must be a JSON object");

      const baseTitle = yt.title?.trim() || String(metaIn.title ?? "").trim() || "未命名";
      const zhTitle = await translateTitleToZh(baseTitle);
      const title = clampText(ensureShurouPrefix(zhTitle || baseTitle), 80) || "未命名";
      const desc = buildBilibiliDesc(yt.description, sourceUrl);

      const metaOut = {
        ...metaIn,
        title,
        desc,
        copyright: 2,
        source: sourceUrl,
      };
      setPublishMetaText(JSON.stringify(metaOut, null, 2));
    },
    [publishMetaText, task, openaiKeySet, translateStyle],
  );

  const isYouTubeTask = task?.source_type === "youtube" && !!(task.source_url ?? "").trim();

  useEffect(() => {
    if (!taskId) return;
    if (!isYouTubeTask) return;
    if (didAutoFillPublishMeta) return;
    if (!publishSettings?.default_meta) return;

    const defaultText = JSON.stringify(publishSettings.default_meta ?? {}, null, 2);
    if (publishMetaText !== defaultText) return;

    (async () => {
      try {
        const yt = await fetchYouTubeMeta();
        if (yt) await applyYouTubeMetaToPublishMeta(yt);
      } catch {
      } finally {
        setDidAutoFillPublishMeta(true);
      }
    })();
  }, [taskId, isYouTubeTask, publishSettings, publishMetaText, didAutoFillPublishMeta, fetchYouTubeMeta, applyYouTubeMetaToPublishMeta]);

  const rawAsset = useMemo(() => {
    const raws = (assets ?? []).filter((x) => x.kind === "video_raw");
    return raws.length ? raws[raws.length - 1] : null;
  }, [assets]);
  const metadataAsset = useMemo(() => {
    const metas = (assets ?? []).filter((x) => x.kind === "metadata_json");
    return metas.length ? metas[metas.length - 1] : null;
  }, [assets]);
  const finalAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "video_final"), [assets]);
  const coverAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "cover_image"), [assets]);

  const shouldPoll = useMemo(() => {
    const hasSubtitleInFlight = (subtitleJobs ?? []).some((j) => j.status === "queued" || j.status === "running");
    const hasPublishInFlight = (publishJobs ?? []).some((j) => j.state === "submitting" || j.state === "submitted");
    return hasSubtitleInFlight || hasPublishInFlight;
  }, [subtitleJobs, publishJobs]);

  useEffect(() => {
    if (!isYouTubeTask) return;
    if (didAutoPickCover) return;
    if (publishCoverKey) return;
    if (!coverAssets.length) return;
    setPublishCoverKey(coverAssets[coverAssets.length - 1].storage_key);
    setDidAutoPickCover(true);
  }, [isYouTubeTask, didAutoPickCover, publishCoverKey, coverAssets]);

  useEffect(() => {
    if (!taskId) return;
    if (!shouldPoll) return;

    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      if (cancelled) return;
      await refresh({ silent: true });
      if (cancelled) return;
      timer = window.setTimeout(tick, 1500);
    };

    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [taskId, shouldPoll, refresh]);

  if (!taskId) return null;

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-semibold">Task Detail</div>
            <div className="mt-1 font-mono text-xs text-slate-600">{taskId}</div>
          </div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>

        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
        {!task ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
        {task ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">Status</div>
              <div className="mt-1">
                <StatusBadge status={task.status} />
              </div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">Source</div>
              <div className="mt-1 text-sm text-slate-800">
                {task.source_type} · {task.source_license}
              </div>
              <div className="mt-1 break-all text-xs text-slate-600">{task.source_url ?? "-"}</div>
            </div>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Upload / Raw Video</div>
        <div className="mt-2 text-xs text-slate-500">已上传：{rawAsset ? rawAsset.storage_key : "无"}</div>
        <div className="mt-1 text-xs text-slate-500">metadata：{metadataAsset ? metadataAsset.storage_key : "无"}</div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept="video/*" onChange={(e) => setVideoFile(e.target.files?.[0] ?? null)} />
          <button
            disabled={!videoFile || busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const fd = new FormData();
                if (!videoFile) throw new Error("no file selected");
                fd.append("file", videoFile, videoFile.name);
                await fetchJson(`${ORCHESTRATOR_URL}/tasks/${taskId}/upload/video`, { method: "POST", body: fd });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            上传
          </button>
          {isYouTubeTask ? (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  const resp = await fetchJson<YouTubeDownloadActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_download`, {
                    method: "POST",
                  });
                  setYoutubeMeta(resp.metadata);
                  if (!publishCoverKey && resp.cover_asset?.storage_key) {
                    setPublishCoverKey(resp.cover_asset.storage_key);
                    setDidAutoPickCover(true);
                  }
                  if (publishSettings?.default_meta) {
                    const defaultText = JSON.stringify(publishSettings.default_meta ?? {}, null, 2);
                    if (publishMetaText === defaultText) await applyYouTubeMetaToPublishMeta(resp.metadata);
                  }
                  await refresh();
                } catch (e: unknown) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              从 YouTube 下载
            </button>
          ) : null}
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Subtitle</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">输出格式</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.srt}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, srt: e.target.checked }))}
                />
                SRT
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.ass}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, ass: e.target.checked }))}
                />
                ASS
              </label>
            </div>
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={burnIn} onChange={(e) => setBurnIn(e.target.checked)} />
                硬字幕（burn-in）
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={softSub} onChange={(e) => setSoftSub(e.target.checked)} />
                软字幕（mkv）
              </label>
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">翻译（可选：mock/noop/openai）</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={translateEnabled} onChange={(e) => setTranslateEnabled(e.target.checked)} />
                启用翻译
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={bilingual} onChange={(e) => setBilingual(e.target.checked)} />
                双语
              </label>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">target_lang</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">provider</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={translateProvider}
                  onChange={(e) => setTranslateProvider(e.target.value)}
                >
                  <option value="mock">mock</option>
                  <option value="noop">noop</option>
                  <option value="openai">openai</option>
                </select>
              </label>

              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">style</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateStyle} onChange={(e) => setTranslateStyle(e.target.value)}>
                  <option value="口语自然">口语自然</option>
                  <option value="正式严谨">正式严谨</option>
                  <option value="电商营销">电商营销</option>
                </select>
              </label>

              <label className="flex items-center gap-2 text-sm md:col-span-2">
                <input
                  type="checkbox"
                  checked={translateEnableSummary}
                  onChange={(e) => setTranslateEnableSummary(e.target.checked)}
                  disabled={translateProvider !== "openai"}
                />
                dynamic summary（仅 openai）
              </label>

              {translateEnabled && translateProvider === "openai" && openaiKeySet === false ? (
                <div className="md:col-span-2 text-xs text-rose-700">
                  OpenAI API Key 未设置，请先到 <Link className="underline" to="/settings/translate">Settings · Translate</Link> 保存配置。
                </div>
              ) : null}
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">ASR（语音识别）</div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">engine</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrEngine} onChange={(e) => setAsrEngine(e.target.value)}>
                  <option value="auto">auto（使用后端默认）</option>
                  <option value="mock">mock</option>
                  <option value="faster-whisper">faster-whisper</option>
                </select>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">language</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={asrLanguage} onChange={(e) => setAsrLanguage(e.target.value)} placeholder="auto / zh / en ..." />
              </label>
              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">model（可选：本地模型目录路径）</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={asrModel}
                  onChange={(e) => setAsrModel(e.target.value)}
                >
                  <option value="">(use env default)</option>
                  {(whisperModels ?? []).map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
                <div className="mt-2 text-xs text-slate-500">
                  提示：可在 Settings → ASR/Whisper 下载/上传模型；选择后会把该路径传给 job 的 `asr.model`。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-3">
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                if (!rawAsset && isYouTubeTask) {
                  const resp = await fetchJson<YouTubeDownloadActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_download`, {
                    method: "POST",
                  });
                  setYoutubeMeta(resp.metadata);
                  if (!publishCoverKey && resp.cover_asset?.storage_key) {
                    setPublishCoverKey(resp.cover_asset.storage_key);
                    setDidAutoPickCover(true);
                  }
                  if (publishSettings?.default_meta) {
                    const defaultText = JSON.stringify(publishSettings.default_meta ?? {}, null, 2);
                    if (publishMetaText === defaultText) await applyYouTubeMetaToPublishMeta(resp.metadata);
                  }
                  await refresh();
                }

                const formats = [
                  subtitleFormats.srt ? "srt" : null,
                  subtitleFormats.ass ? "ass" : null,
                ].filter(Boolean);
                const resp = await fetchJson<SubtitleActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/subtitle`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    formats,
                    burn_in: burnIn,
                    soft_sub: softSub,
                    ass_style: "clean_white",
                    asr_engine: asrEngine,
                    asr_language: asrLanguage,
                    asr_model: asrModel.trim() ? asrModel.trim() : null,
                    translate_enabled: translateEnabled,
                    translate_provider: translateProvider,
                    target_lang: targetLang,
                    translate_style: translateStyle,
                    translate_enable_summary: translateProvider === "openai" ? translateEnableSummary : null,
                    bilingual,
                  }),
                });
                await refresh();
                alert(`已提交字幕任务：${resp.job_id}`);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            生成字幕
          </button>
        </div>

        <div className="mt-4">
          <div className="text-xs font-semibold text-slate-700">Subtitle Jobs</div>
          {!subtitleJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {subtitleJobs && subtitleJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {subtitleJobs && subtitleJobs.length > 0 ? (
            <div className="mt-2 overflow-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">ID</th>
                    <th className="py-2 pr-3">Status</th>
                    <th className="py-2 pr-3">Progress</th>
                    <th className="py-2 pr-3">Error</th>
                    <th className="py-2 pr-3">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {subtitleJobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3">{j.status}</td>
                      <td className="py-2 pr-3">{j.progress}%</td>
                      <td
                        className={`py-2 pr-3 text-xs ${j.error_message ? "max-w-[360px] truncate text-rose-700" : "text-slate-400"}`}
                        title={j.error_message ?? ""}
                      >
                        {j.error_message ? clampText(j.error_message, 160) : "—"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Assets</div>
        {!assets ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {assets ? (
          <div className="mt-2 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Kind</th>
                  <th className="py-2 pr-3">Key</th>
                  <th className="py-2 pr-3">Created</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {assets.map((a) => (
                  <tr key={a.id} className="border-t">
                    <td className="py-2 pr-3 text-xs">{a.kind}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{a.storage_key}</td>
                    <td className="py-2 pr-3 text-xs text-slate-600">{new Date(a.created_at).toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}/download`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Download
                        </a>
                        {a.kind === "video_final" ? (
                          <button
                            disabled={busy}
                            className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                            onClick={async () => {
                              if (!confirm("确定删除该最终视频资产？（会从存储中删除）")) return;
                              setBusy(true);
                              setError(null);
                              try {
                                await fetchJson(`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}`, { method: "DELETE" });
                                await refresh();
                              } catch (e: unknown) {
                                setError(e instanceof Error ? e.message : String(e));
                              } finally {
                                setBusy(false);
                              }
                            }}
                          >
                            Delete
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {finalAssets.length > 0 ? (
          <div className="mt-3 rounded border bg-slate-50 p-3 text-xs text-slate-700">
            <div className="font-semibold text-slate-700">最终视频</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {finalAssets.map((a) => (
                <a
                  key={a.id}
                  className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-800"
                  href={`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}/download`}
                  target="_blank"
                  rel="noreferrer"
                >
                  下载：{a.storage_key.split("/").slice(-1)[0]}
                </a>
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Publish（Bilibili）</div>
        <div className="mt-2 text-xs text-slate-500">
          当后端 <span className="font-mono">BILIBILI_PUBLISH_MODE=mock</span> 时仅返回模拟结果；设置为 <span className="font-mono">web</span>（或任意非 mock）则走真实投稿（需先在 Settings · Bilibili 保存 Cookies 并测试登录）。
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">video_key（可选）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={publishVideoKey}
              onChange={(e) => setPublishVideoKey(e.target.value)}
            >
              <option value="">自动（最新 video_final）</option>
              {[...finalAssets].reverse().map((a) => (
                <option key={a.id} value={a.storage_key}>
                  {a.storage_key}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">cover_key（可选）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={publishCoverKey}
              onChange={(e) => {
                setPublishCoverKey(e.target.value);
                setDidAutoPickCover(true);
              }}
            >
              <option value="">不使用封面</option>
              {[...coverAssets].reverse().map((a) => (
                <option key={a.id} value={a.storage_key}>
                  {a.storage_key}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept="image/*" onChange={(e) => setCoverFile(e.target.files?.[0] ?? null)} />
          <button
            disabled={busy || !coverFile}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const fd = new FormData();
                if (!coverFile) throw new Error("no cover selected");
                fd.append("file", coverFile, coverFile.name);
                const a = await fetchJson<Asset>(`${ORCHESTRATOR_URL}/tasks/${taskId}/upload/cover`, { method: "POST", body: fd });
                setPublishCoverKey(a.storage_key);
                setDidAutoPickCover(true);
                setCoverFile(null);
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            上传封面
          </button>
        </div>

        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-600">
            <div>meta.json</div>
            <div className="flex items-center gap-2">
              <button
                disabled={busy || !publishSettings?.default_meta}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={() => setPublishMetaText(JSON.stringify(publishSettings?.default_meta ?? {}, null, 2))}
              >
                加载默认
              </button>
              <button
                disabled={busy || !isYouTubeTask}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    const yt = youtubeMeta ?? (await fetchYouTubeMeta());
                    if (!yt) throw new Error("failed to fetch youtube metadata");
                    await applyYouTubeMetaToPublishMeta(yt);
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                从 YouTube 填充
              </button>
              <button
                disabled={busy}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={() => {
                  try {
                    const meta = JSON.parse(publishMetaText);
                    setPublishMetaText(JSON.stringify(meta, null, 2));
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                }}
              >
                格式化
              </button>
            </div>
          </div>
          <textarea
            className="h-64 w-full rounded border p-3 font-mono text-xs"
            value={publishMetaText}
            onChange={(e) => setPublishMetaText(e.target.value)}
          />
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const metaIn = JSON.parse(publishMetaText);
                if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) throw new Error("meta must be a JSON object");
                const meta =
                  publishSettings?.default_meta && typeof publishSettings.default_meta === "object" && !Array.isArray(publishSettings.default_meta)
                    ? { ...publishSettings.default_meta, ...metaIn }
                    : metaIn;
                const resp = await fetchJson<PublishResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/publish`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    account_id: null,
                    video_key: publishVideoKey || null,
                    cover_key: publishCoverKey || null,
                    meta,
                  }),
                });
                await refresh();
                alert(`Publish state=${resp.state} bvid=${resp.bvid ?? "-"}`);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            投稿
          </button>
          <Link to="/tasks" className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            返回列表
          </Link>
        </div>

        <div className="mt-4">
          <div className="text-xs font-semibold text-slate-700">Publish Jobs</div>
          {!publishJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {publishJobs && publishJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {publishJobs && publishJobs.length > 0 ? (
            <div className="mt-2 overflow-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">ID</th>
                    <th className="py-2 pr-3">State</th>
                    <th className="py-2 pr-3">bvid</th>
                    <th className="py-2 pr-3">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {publishJobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3">{j.state}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.bvid ?? "-"}</td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
