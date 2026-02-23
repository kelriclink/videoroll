import { Link } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type AutoProfile = {
  formats: string[];
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  video_codec: string;

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
  publish_typeid_mode: string;
  publish_title_prefix: string;
  publish_translate_title: boolean;
  publish_use_youtube_cover: boolean;
};

export default function SettingsAutoPage() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [whisperModels, setWhisperModels] = useState<Array<{ name: string; path: string }> | null>(null);
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);

  const [formats, setFormats] = useState<{ srt: boolean; ass: boolean }>({ srt: true, ass: true });
  const [burnIn, setBurnIn] = useState(true);
  const [softSub, setSoftSub] = useState(false);
  const [assStyle, setAssStyle] = useState("clean_white");
  const [videoCodec, setVideoCodec] = useState("av1");

  const [asrEngine, setAsrEngine] = useState("auto");
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrModel, setAsrModel] = useState("");

  const [translateEnabled, setTranslateEnabled] = useState(true);
  const [bilingual, setBilingual] = useState(false);
  const [targetLang, setTargetLang] = useState("zh");
  const [translateProvider, setTranslateProvider] = useState("openai");
  const [translateStyle, setTranslateStyle] = useState("口语自然");
  const [translateEnableSummary, setTranslateEnableSummary] = useState(true);

  const [autoPublish, setAutoPublish] = useState(true);
  const [publishTypeidMode, setPublishTypeidMode] = useState("ai_summary");
  const [publishTranslateTitle, setPublishTranslateTitle] = useState(true);
  const [publishTitlePrefix, setPublishTitlePrefix] = useState("【熟肉】");
  const [publishUseYouTubeCover, setPublishUseYouTubeCover] = useState(true);

  async function refresh() {
    setError(null);
    try {
      const [profile, models, translateSettings] = await Promise.all([
        fetchJson<AutoProfile>(`${SUBTITLE_SERVICE_URL}/subtitle/auto/profile`),
        fetchJson<Array<{ name: string; path: string }>>(`${SUBTITLE_SERVICE_URL}/subtitle/models`).catch(() => null),
        fetchJson<{ openai_api_key_set: boolean }>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`).catch(() => null),
      ]);

      if (models) setWhisperModels(models);
      if (translateSettings) setOpenaiKeySet(Boolean(translateSettings.openai_api_key_set));

      const f = Array.isArray(profile.formats) ? profile.formats : [];
      setFormats({ srt: f.includes("srt"), ass: f.includes("ass") });
      setBurnIn(Boolean(profile.burn_in));
      setSoftSub(Boolean(profile.soft_sub));
      setAssStyle(profile.ass_style || "clean_white");
      setVideoCodec((profile.video_codec || "av1").toLowerCase());

      setAsrEngine(profile.asr_engine || "auto");
      setAsrLanguage(profile.asr_language || "auto");
      setAsrModel((profile.asr_model ?? "").trim());

      setTranslateEnabled(Boolean(profile.translate_enabled));
      setBilingual(Boolean(profile.bilingual));
      setTargetLang(profile.target_lang || "zh");
      setTranslateProvider(profile.translate_provider || "openai");
      setTranslateStyle(profile.translate_style || "口语自然");
      setTranslateEnableSummary(Boolean(profile.translate_enable_summary));

      setAutoPublish(Boolean(profile.auto_publish));
      setPublishTypeidMode((profile.publish_typeid_mode || "ai_summary").toLowerCase());
      setPublishTranslateTitle(Boolean(profile.publish_translate_title));
      setPublishTitlePrefix((profile.publish_title_prefix ?? "【熟肉】").trim() || "【熟肉】");
      setPublishUseYouTubeCover(Boolean(profile.publish_use_youtube_cover));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const formatsOut = useMemo(
    () => [formats.srt ? "srt" : null, formats.ass ? "ass" : null].filter(Boolean) as string[],
    [formats],
  );

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Settings · Auto Mode</div>
        <div className="mt-1 text-sm text-slate-600">用于 “YouTube 自动模式” 的默认参数（下载→字幕/翻译→烧录→投稿）。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-semibold">Subtitle</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">输出格式</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={formats.srt}
                  onChange={(e) => setFormats((v) => ({ ...v, srt: e.target.checked }))}
                />
                SRT
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={formats.ass}
                  onChange={(e) => setFormats((v) => ({ ...v, ass: e.target.checked }))}
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
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_codec（硬字幕输出编码）</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={videoCodec} onChange={(e) => setVideoCodec(e.target.value)}>
                  <option value="av1">av1（体积更小，编码更慢）</option>
                  <option value="h264">h264（兼容更好，编码更快）</option>
                </select>
              </label>
              <div className="mt-2 text-xs text-slate-500">提示：只有在启用 “硬字幕（burn-in）” 时才会用到该编码设置。</div>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">ass_style</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={assStyle} onChange={(e) => setAssStyle(e.target.value)}>
                  <option value="clean_white">clean_white</option>
                </select>
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
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateProvider} onChange={(e) => setTranslateProvider(e.target.value)}>
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

          <div className="rounded border p-3 md:col-span-2">
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
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrModel} onChange={(e) => setAsrModel(e.target.value)}>
                  <option value="">(use default)</option>
                  {(whisperModels ?? []).map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
                <div className="mt-2 text-xs text-slate-500">
                  提示：留空表示使用 Settings · ASR 中的默认模型（或后端 env 默认）。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-4 rounded border p-3">
          <div className="text-xs font-semibold text-slate-700">Publish（Bilibili）</div>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={autoPublish} onChange={(e) => setAutoPublish(e.target.checked)} />
              自动投稿
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={publishUseYouTubeCover}
                onChange={(e) => setPublishUseYouTubeCover(e.target.checked)}
              />
              使用 YouTube 封面
            </label>
            <label className="block md:col-span-2">
              <div className="mb-1 text-xs text-slate-600">分区模式（typeid_mode）</div>
              <select
                className="w-full rounded border px-3 py-2 text-sm"
                value={publishTypeidMode}
                onChange={(e) => setPublishTypeidMode(e.target.value)}
              >
                <option value="ai_summary">AI（根据字幕总结）</option>
                <option value="bilibili_predict">B站预测（标题/文件）</option>
                <option value="meta">手动（使用 Settings · Bilibili 的 meta.typeid）</option>
              </select>
              {publishTypeidMode === "ai_summary" && (!translateEnableSummary || translateProvider !== "openai" || openaiKeySet === false) ? (
                <div className="mt-2 text-xs text-rose-700">
                  提示：AI 分区需要启用 OpenAI summary，并在 Settings · Translate 保存 OpenAI API Key；否则会回退到 B 站预测/手动分区。
                </div>
              ) : (
                <div className="mt-2 text-xs text-slate-500">AI 分区会在投稿时自动拉取可用分区列表，让 AI 从候选中选择一个 typeid。</div>
              )}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={publishTranslateTitle}
                onChange={(e) => setPublishTranslateTitle(e.target.checked)}
              />
              标题自动翻译
            </label>
            <label className="block">
              <div className="mb-1 text-xs text-slate-600">标题前缀</div>
              <input
                className="w-full rounded border px-3 py-2 text-sm"
                value={publishTitlePrefix}
                onChange={(e) => setPublishTitlePrefix(e.target.value)}
                placeholder="【熟肉】"
              />
            </label>
          </div>
          <div className="mt-2 text-xs text-slate-500">
            投稿 meta 的默认值（标题/简介/tags 等）请到 <Link className="underline" to="/settings/bilibili">Settings · Bilibili</Link> 配置；分区由上面的 “分区模式” 决定。
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                if (!formatsOut.length) throw new Error("至少选择一种输出格式");
                await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/auto/profile`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    formats: formatsOut,
                    burn_in: burnIn,
                    soft_sub: softSub,
                    ass_style: assStyle,
                    video_codec: videoCodec,
                    asr_engine: asrEngine,
                    asr_language: asrLanguage,
                    asr_model: asrModel.trim() ? asrModel.trim() : "",
                    translate_enabled: translateEnabled,
                    translate_provider: translateProvider,
                    target_lang: targetLang,
                    translate_style: translateStyle,
                    translate_enable_summary: translateEnableSummary,
                    bilingual,
                    auto_publish: autoPublish,
                    publish_typeid_mode: publishTypeidMode,
                    publish_title_prefix: publishTitlePrefix,
                    publish_translate_title: publishTranslateTitle,
                    publish_use_youtube_cover: publishUseYouTubeCover,
                  }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "保存中…" : "保存"}
          </button>

          <button
            disabled={busy}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={() => {
              if (!confirm("确定恢复默认配置吗？")) return;
              setFormats({ srt: true, ass: true });
              setBurnIn(true);
              setSoftSub(false);
              setAssStyle("clean_white");
              setVideoCodec("av1");
              setAsrEngine("auto");
              setAsrLanguage("auto");
              setAsrModel("");
              setTranslateEnabled(true);
              setBilingual(false);
              setTargetLang("zh");
              setTranslateProvider("openai");
              setTranslateStyle("口语自然");
              setTranslateEnableSummary(true);
              setAutoPublish(true);
              setPublishTypeidMode("ai_summary");
              setPublishTranslateTitle(true);
              setPublishTitlePrefix("【熟肉】");
              setPublishUseYouTubeCover(true);
            }}
          >
            恢复默认（未保存）
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4 text-xs text-slate-600">
        <div className="font-semibold text-slate-700">提示</div>
        <div className="mt-2">
          - OpenAI 相关参数（base_url/model/timeout/api_key）在 <Link className="underline" to="/settings/translate">Settings · Translate</Link> 配置。
        </div>
        <div className="mt-1">
          - ASR 默认模型在 <Link className="underline" to="/settings/asr">Settings · ASR</Link> 配置。
        </div>
      </div>
    </div>
  );
}
