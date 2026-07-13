import { Link } from "react-router-dom";
import { useEffect, useMemo, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type YouTubeSubtitleMode = "off" | "target" | "auto_source";

type AutoProfile = {
  formats: string[];
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  video_codec: string;
  use_intel_gpu: boolean;
  video_preset?: string | null;
  video_crf?: number | null;
  primary_font_scale_percent?: number | null;
  secondary_font_scale_percent?: number | null;

  asr_engine: string;
  asr_language: string;
  asr_model?: string | null;

  prefer_youtube_subtitles: boolean;
  youtube_subtitle_mode?: YouTubeSubtitleMode | null;
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
  publish_enable_reprint: boolean;
};

type IntelHardwareProbe = {
  checked: boolean;
  available: boolean;
  render_device: string;
  model_name?: string | null;
  driver?: string | null;
  pci_slot?: string | null;
  pci_id?: string | null;
  detail: string;
};

function normalizeYouTubeSubtitleMode(value: unknown, legacyPrefer?: boolean | null): YouTubeSubtitleMode {
  const mode = String(value ?? "").trim().toLowerCase();
  if (mode === "off" || mode === "target" || mode === "auto_source") return mode;
  if (legacyPrefer === false) return "off";
  return "target";
}

export default function SettingsAutoPage() {
  const confirm = useConfirm();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [whisperModels, setWhisperModels] = useState<Array<{ name: string; path: string }> | null>(null);
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);

  const [formats, setFormats] = useState<{ srt: boolean; ass: boolean }>({ srt: true, ass: true });
  const [burnIn, setBurnIn] = useState(true);
  const [softSub, setSoftSub] = useState(false);
  const [assStyle, setAssStyle] = useState("clean_white");
  const [videoCodec, setVideoCodec] = useState("av1");
  const [useIntelGpu, setUseIntelGpu] = useState(false);
  const [intelProbeBusy, setIntelProbeBusy] = useState(false);
  const [intelProbe, setIntelProbe] = useState<IntelHardwareProbe | null>(null);
  const [videoPresetText, setVideoPresetText] = useState<string>("");
  const [videoCrfText, setVideoCrfText] = useState<string>("");
  const [primaryFontScalePercentText, setPrimaryFontScalePercentText] = useState<string>("100");
  const [secondaryFontScalePercentText, setSecondaryFontScalePercentText] = useState<string>("100");

  const [asrEngine, setAsrEngine] = useState("auto");
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrModel, setAsrModel] = useState("");

  const [youtubeSubtitleMode, setYouTubeSubtitleMode] = useState<YouTubeSubtitleMode>("target");
  const [translateEnabled, setTranslateEnabled] = useState(true);
  const [bilingual, setBilingual] = useState(false);
  const [targetLang, setTargetLang] = useState("zh");
  const [translateProvider, setTranslateProvider] = useState("openai");
  const [translateStyle, setTranslateStyle] = useState("口语自然");
  const [translateEnableSummary, setTranslateEnableSummary] = useState(true);

  const [autoPublish, setAutoPublish] = useState(true);
  const [enabledPlatforms, setEnabledPlatforms] = useState<string[]>([]);
  const [publishTypeidMode, setPublishTypeidMode] = useState("ai_summary");
  const [publishTranslateTitle, setPublishTranslateTitle] = useState(true);
  const [publishTitlePrefix, setPublishTitlePrefix] = useState("【熟肉】");
  const [publishUseYouTubeCover, setPublishUseYouTubeCover] = useState(true);
  const [publishEnableReprint, setPublishEnableReprint] = useState(true);

  async function refresh() {
    setError(null);
    try {
      const [profile, models, translateSettings, platformSettingsResp] = await Promise.all([
        fetchJson<AutoProfile>(`${ORCHESTRATOR_URL}/subtitle/auto/profile`),
        fetchJson<Array<{ name: string; path: string }>>(`${ORCHESTRATOR_URL}/subtitle/models`).catch(() => null),
        fetchJson<{ openai_api_key_set: boolean }>(`${ORCHESTRATOR_URL}/subtitle/translate/settings`).catch(() => null),
        fetchJson<{ platforms: Record<string, boolean> }>(`${ORCHESTRATOR_URL}/settings/publish/platforms`).catch(() => null),
      ]);

      if (models) setWhisperModels(models);
      if (translateSettings) setOpenaiKeySet(Boolean(translateSettings.openai_api_key_set));
      if (platformSettingsResp?.platforms) {
        setEnabledPlatforms(Object.entries(platformSettingsResp.platforms).filter(([, v]) => v).map(([k]) => k));
      }

      const f = Array.isArray(profile.formats) ? profile.formats : [];
      setFormats({ srt: f.includes("srt"), ass: f.includes("ass") });
      setBurnIn(Boolean(profile.burn_in));
      setSoftSub(Boolean(profile.soft_sub));
      setAssStyle(profile.ass_style || "clean_white");
      setVideoCodec((profile.video_codec || "av1").toLowerCase());
      setUseIntelGpu(Boolean(profile.use_intel_gpu));
      setVideoPresetText(typeof profile.video_preset === "string" ? profile.video_preset : "");
      setVideoCrfText(typeof profile.video_crf === "number" ? String(profile.video_crf) : "");
      setPrimaryFontScalePercentText(typeof profile.primary_font_scale_percent === "number" ? String(profile.primary_font_scale_percent) : "100");
      setSecondaryFontScalePercentText(typeof profile.secondary_font_scale_percent === "number" ? String(profile.secondary_font_scale_percent) : "100");

      setAsrEngine(profile.asr_engine || "auto");
      setAsrLanguage(profile.asr_language || "auto");
      setAsrModel((profile.asr_model ?? "").trim());

      setYouTubeSubtitleMode(normalizeYouTubeSubtitleMode(profile.youtube_subtitle_mode, profile.prefer_youtube_subtitles));
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
      setPublishEnableReprint(Boolean(profile.publish_enable_reprint));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!useIntelGpu) setIntelProbe(null);
  }, [useIntelGpu]);

  const formatsOut = useMemo(
    () => [formats.srt ? "srt" : null, formats.ass ? "ass" : null].filter(Boolean) as string[],
    [formats],
  );

  async function detectIntelHardware() {
    setIntelProbeBusy(true);
    try {
      const probe = await fetchJson<IntelHardwareProbe>(`${ORCHESTRATOR_URL}/subtitle/hardware/intel`);
      setIntelProbe(probe);
    } catch (e: unknown) {
      setIntelProbe({
        checked: true,
        available: false,
        render_device: "/dev/dri/renderD128",
        detail: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setIntelProbeBusy(false);
    }
  }

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
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={useIntelGpu}
                  onChange={(e) => setUseIntelGpu(e.target.checked)}
                />
                启用 Intel iGPU 硬件编码
              </label>
            </div>
            {useIntelGpu ? (
              <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-3">
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    onClick={() => detectIntelHardware()}
                    disabled={intelProbeBusy}
                    className="rounded border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {intelProbeBusy ? "检测中..." : "检测硬件"}
                  </button>
                  {intelProbe?.available && intelProbe.model_name ? (
                    <div className="text-sm text-emerald-700">已检测到：{intelProbe.model_name}</div>
                  ) : null}
                  {intelProbe && !intelProbe.available ? (
                    <div className="text-sm text-amber-700">未检测到可用 Intel 硬件</div>
                  ) : null}
                </div>
                {intelProbe ? (
                  <div className="mt-2 text-xs text-slate-600">
                    <div>{intelProbe.detail || (intelProbe.available ? "已检测到 Intel 硬件" : "当前未检测到可用 Intel 硬件")}</div>
                    <div className="mt-1">
                      设备：{intelProbe.render_device || "-"}
                      {intelProbe.driver ? ` · 驱动：${intelProbe.driver}` : ""}
                      {intelProbe.pci_slot ? ` · PCI：${intelProbe.pci_slot}` : ""}
                      {intelProbe.pci_id ? ` · ID：${intelProbe.pci_id}` : ""}
                    </div>
                  </div>
                ) : (
                  <div className="mt-2 text-xs text-slate-500">
                    点击“检测硬件”后，会显示当前容器可见的 Intel 显卡型号。
                  </div>
                )}
              </div>
            ) : null}
            <div className="mt-2 text-xs text-slate-500">
              提示：这里只加速硬字幕的最终视频编码。字幕烧录本身仍是 CPU 过滤；软字幕只是封装，不走 GPU。当前 Intel 路径支持 h264/av1。
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_crf（可选：留空=默认）</div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoCrfText}
                  onChange={(e) => setVideoCrfText(e.target.value)}
                  placeholder={videoCodec === "h264" ? "默认 18（h264）" : "默认 24（av1）"}
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">提示：CRF 越小质量越高、体积越大、编码越慢。常用范围：h264 18~28；av1 24~35。</div>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_preset（可选：留空=默认）</div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoPresetText}
                  onChange={(e) => setVideoPresetText(e.target.value)}
                  placeholder={
                    useIntelGpu
                      ? videoCodec === "h264"
                        ? "留空=驱动默认（Intel h264）"
                        : "留空=驱动默认（Intel av1）；可填 0..13"
                      : videoCodec === "h264"
                        ? "默认 veryfast（h264）"
                        : "默认 4（av1, 0..13 越小越慢）"
                  }
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">
                提示：CPU h264 可用 ultrafast..veryslow；CPU av1（SVT）为 0..13。Intel GPU 开启时，h264 文本 preset 和 av1 的 0..13 都会映射到 VAAPI quality。
              </div>
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">主字幕字号（%）</div>
                <input
                  type="number"
                  min={25}
                  max={300}
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={primaryFontScalePercentText}
                  onChange={(e) => setPrimaryFontScalePercentText(e.target.value)}
                  placeholder="100"
                />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">辅字幕字号（%）</div>
                <input
                  type="number"
                  min={25}
                  max={300}
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={secondaryFontScalePercentText}
                  onChange={(e) => setSecondaryFontScalePercentText(e.target.value)}
                  placeholder="100"
                />
              </label>
            </div>
            <div className="mt-2 text-xs text-slate-500">
              提示：`100` 表示保持当前默认字号；主/辅字幕可以分别调节。这里设置的是相对当前自适应字号的百分比，会继续按视频分辨率等比缩放。
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
              <label className="block min-w-64">
                <div className="mb-1 text-xs text-slate-600">YouTube 字幕复用</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={youtubeSubtitleMode}
                  onChange={(e) => setYouTubeSubtitleMode(normalizeYouTubeSubtitleMode(e.target.value))}
                >
                  <option value="off">关闭，直接走 ASR</option>
                  <option value="target">优先目标语言字幕</option>
                  <option value="auto_source">优先自动生成原语言字幕</option>
                </select>
              </label>
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

              <div className="md:col-span-2 text-xs text-slate-500">
                {youtubeSubtitleMode === "off"
                  ? "逻辑：不复用 YouTube 字幕，直接进入 ASR；若启用翻译，则在 ASR 结果上继续翻译。"
                  : youtubeSubtitleMode === "auto_source"
                    ? "逻辑：优先抓取 YouTube 自动生成的原语言字幕；若启用翻译，则直接进入翻译管线；如果没有可用自动字幕，再回退到 ASR。"
                    : "逻辑：优先找 `target_lang` 对应的 YouTube 字幕；命中后直接复用并跳过翻译；如果没有可用目标字幕，再回退到 ASR。"}
              </div>

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
                  <option value="openvino">openvino（方案2 / Intel Arc）</option>
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
                  提示：留空表示使用 Settings · ASR 中的默认模型。若选择 `openvino`，这里应指向已导出的 OpenVINO Whisper 模型目录。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-4 rounded border p-3">
          <div className="text-xs font-semibold text-slate-700">投稿（多平台）</div>
          {enabledPlatforms.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {enabledPlatforms.map((p) => (
                <span key={p} className="inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-800">
                  {p === "bilibili" ? "哔哩哔哩" : p === "douyin" ? "抖音" : p === "xiaohongshu" ? "小红书" : p === "kuaishou" ? "快手" : p}
                </span>
              ))}
              <span className="text-xs text-slate-500">自动模式将投稿到以上平台</span>
            </div>
          )}
          {enabledPlatforms.length === 0 && (
            <div className="mt-1 text-xs text-amber-600">未启用任何投稿平台，请先到投稿设置中勾选</div>
          )}
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={autoPublish} onChange={(e) => setAutoPublish(e.target.checked)} />
              自动投稿
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={publishEnableReprint}
                onChange={(e) => setPublishEnableReprint(e.target.checked)}
              />
              启用转载（copyright=2）
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
                <option value="meta">手动（使用投稿设置里的 meta.typeid）</option>
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
            投稿 meta 的默认值（标题/简介/tags 等）请到 <Link className="underline" to="/settings/publish">投稿设置</Link> 配置；分区由上面的 “分区模式” 决定。
          </div>
          <div className="mt-1 text-xs text-slate-500">
            投稿前 AI 审核规则请到 <Link className="underline" to="/settings/review">Settings · Review</Link> 配置。
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
                const crfRaw = videoCrfText.trim();
                let video_crf: number | null = null;
                if (crfRaw) {
                  const n = Number(crfRaw);
                  if (!Number.isFinite(n) || !Number.isInteger(n)) throw new Error("video_crf 必须是整数");
                  video_crf = n;
                }
                const primaryFontScaleRaw = primaryFontScalePercentText.trim();
                const primary_font_scale_percent = Number(primaryFontScaleRaw || "100");
                if (!Number.isFinite(primary_font_scale_percent) || !Number.isInteger(primary_font_scale_percent)) {
                  throw new Error("主字幕字号必须是整数百分比");
                }
                if (primary_font_scale_percent < 25 || primary_font_scale_percent > 300) {
                  throw new Error("主字幕字号百分比必须在 25~300 之间");
                }
                const secondaryFontScaleRaw = secondaryFontScalePercentText.trim();
                const secondary_font_scale_percent = Number(secondaryFontScaleRaw || "100");
                if (!Number.isFinite(secondary_font_scale_percent) || !Number.isInteger(secondary_font_scale_percent)) {
                  throw new Error("辅字幕字号必须是整数百分比");
                }
                if (secondary_font_scale_percent < 25 || secondary_font_scale_percent > 300) {
                  throw new Error("辅字幕字号百分比必须在 25~300 之间");
                }
                const presetRaw = videoPresetText.trim();
                await fetchJson(`${ORCHESTRATOR_URL}/subtitle/auto/profile`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    formats: formatsOut,
                    burn_in: burnIn,
                    soft_sub: softSub,
                    ass_style: assStyle,
                    video_codec: videoCodec,
                    use_intel_gpu: useIntelGpu,
                    video_preset: presetRaw ? presetRaw : null,
                    video_crf,
                    primary_font_scale_percent,
                    secondary_font_scale_percent,
                    asr_engine: asrEngine,
                    asr_language: asrLanguage,
                    asr_model: asrModel.trim() ? asrModel.trim() : "",
                    prefer_youtube_subtitles: youtubeSubtitleMode !== "off",
                    youtube_subtitle_mode: youtubeSubtitleMode,
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
                    publish_enable_reprint: publishEnableReprint,
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
            onClick={async () => {
              const ok = await confirm({
                title: "恢复默认配置",
                message: "确定恢复默认配置吗？当前页面中尚未保存的修改会被覆盖。",
                confirmLabel: "恢复默认",
                tone: "warning",
              });
              if (!ok) return;
              setFormats({ srt: true, ass: true });
              setBurnIn(true);
              setSoftSub(false);
              setAssStyle("clean_white");
              setVideoCodec("av1");
              setVideoPresetText("");
              setVideoCrfText("");
              setPrimaryFontScalePercentText("100");
              setSecondaryFontScalePercentText("100");
              setAsrEngine("auto");
              setAsrLanguage("auto");
              setAsrModel("");
              setYouTubeSubtitleMode("target");
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
              setPublishEnableReprint(true);
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
