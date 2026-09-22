import { Link } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { Button, SettingsSaveBar } from "../components/ui";
import { useUnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type YouTubeSubtitleMode = "off" | "target" | "auto_source";
type PublishPlatform = "bilibili" | "douyin" | "xiaohongshu" | "kuaishou";

const PUBLISH_PLATFORMS: Array<{ id: PublishPlatform; label: string }> = [
  { id: "bilibili", label: "哔哩哔哩" },
  { id: "douyin", label: "抖音" },
  { id: "xiaohongshu", label: "小红书" },
  { id: "kuaishou", label: "快手" },
];

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
  auto_publish_platforms: PublishPlatform[];
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

function autoProfileSnapshotFromServer(profile: AutoProfile, enabledPlatforms: PublishPlatform[] | null): string {
  const formats = Array.isArray(profile.formats) ? profile.formats : [];
  const platforms = Array.isArray(profile.auto_publish_platforms) ? profile.auto_publish_platforms : [];
  return JSON.stringify({
    formats: { srt: formats.includes("srt"), ass: formats.includes("ass") },
    burnIn: Boolean(profile.burn_in),
    softSub: Boolean(profile.soft_sub),
    assStyle: profile.ass_style || "clean_white",
    videoCodec: (profile.video_codec || "av1").toLowerCase(),
    useIntelGpu: Boolean(profile.use_intel_gpu),
    videoPresetText: typeof profile.video_preset === "string" ? profile.video_preset : "",
    videoCrfText: typeof profile.video_crf === "number" ? String(profile.video_crf) : "",
    primaryFontScalePercentText:
      typeof profile.primary_font_scale_percent === "number" ? String(profile.primary_font_scale_percent) : "100",
    secondaryFontScalePercentText:
      typeof profile.secondary_font_scale_percent === "number" ? String(profile.secondary_font_scale_percent) : "100",
    asrEngine: profile.asr_engine || "auto",
    asrLanguage: profile.asr_language || "auto",
    asrModel: (profile.asr_model ?? "").trim(),
    youtubeSubtitleMode: normalizeYouTubeSubtitleMode(profile.youtube_subtitle_mode, profile.prefer_youtube_subtitles),
    translateEnabled: Boolean(profile.translate_enabled),
    bilingual: Boolean(profile.bilingual),
    targetLang: profile.target_lang || "zh",
    translateProvider: profile.translate_provider || "openai",
    translateStyle: profile.translate_style || "口语自然",
    translateEnableSummary: Boolean(profile.translate_enable_summary),
    autoPublish: Boolean(profile.auto_publish),
    autoPublishPlatforms: platforms
      .filter((platform) => enabledPlatforms === null || enabledPlatforms.includes(platform))
      .sort(),
    publishTypeidMode: (profile.publish_typeid_mode || "ai_summary").toLowerCase(),
    publishTranslateTitle: Boolean(profile.publish_translate_title),
    publishTitlePrefix: (profile.publish_title_prefix ?? "【熟肉】").trim() || "【熟肉】",
    publishUseYouTubeCover: Boolean(profile.publish_use_youtube_cover),
    publishEnableReprint: Boolean(profile.publish_enable_reprint),
  });
}

export default function SettingsAutoPage() {
  const confirm = useConfirm();
  const savedSnapshotRef = useRef("");
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
  const [enabledPlatforms, setEnabledPlatforms] = useState<PublishPlatform[]>([]);
  const [enabledPlatformsLoaded, setEnabledPlatformsLoaded] = useState(false);
  const [autoPublishPlatforms, setAutoPublishPlatforms] = useState<PublishPlatform[]>([]);
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
      let enabledPlatformIds: PublishPlatform[] | null = null;
      if (platformSettingsResp?.platforms) {
        enabledPlatformIds = PUBLISH_PLATFORMS.filter(({ id }) => platformSettingsResp.platforms[id] === true).map(({ id }) => id);
        setEnabledPlatforms(enabledPlatformIds);
        setEnabledPlatformsLoaded(true);
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
      setAutoPublishPlatforms(Array.isArray(profile.auto_publish_platforms) ? profile.auto_publish_platforms : []);
      setPublishTypeidMode((profile.publish_typeid_mode || "ai_summary").toLowerCase());
      setPublishTranslateTitle(Boolean(profile.publish_translate_title));
      setPublishTitlePrefix((profile.publish_title_prefix ?? "【熟肉】").trim() || "【熟肉】");
      setPublishUseYouTubeCover(Boolean(profile.publish_use_youtube_cover));
      setPublishEnableReprint(Boolean(profile.publish_enable_reprint));
      savedSnapshotRef.current = autoProfileSnapshotFromServer(profile, enabledPlatformIds);
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

  const currentSnapshot = JSON.stringify({
    formats,
    burnIn,
    softSub,
    assStyle,
    videoCodec,
    useIntelGpu,
    videoPresetText,
    videoCrfText,
    primaryFontScalePercentText,
    secondaryFontScalePercentText,
    asrEngine,
    asrLanguage,
    asrModel: asrModel.trim(),
    youtubeSubtitleMode,
    translateEnabled,
    bilingual,
    targetLang,
    translateProvider,
    translateStyle,
    translateEnableSummary,
    autoPublish,
    autoPublishPlatforms: [...(enabledPlatformsLoaded ? autoPublishPlatforms.filter((platform) => enabledPlatforms.includes(platform)) : autoPublishPlatforms)].sort(),
    publishTypeidMode,
    publishTranslateTitle,
    publishTitlePrefix: publishTitlePrefix.trim() || "【熟肉】",
    publishUseYouTubeCover,
    publishEnableReprint,
  });
  const isDirty = Boolean(savedSnapshotRef.current) && savedSnapshotRef.current !== currentSnapshot;
  useUnsavedChangesGuard(isDirty, { message: "离开当前页面会丢失尚未保存的自动模式配置。" });

  async function saveProfile() {
    setBusy(true);
    setError(null);
    try {
      if (!formatsOut.length) throw new Error("至少选择一种输出格式");
      const availableAutoPublishPlatforms = enabledPlatformsLoaded
        ? autoPublishPlatforms.filter((platform) => enabledPlatforms.includes(platform))
        : autoPublishPlatforms;
      const crfRaw = videoCrfText.trim();
      let video_crf: number | null = null;
      if (crfRaw) {
        const n = Number(crfRaw);
        if (!Number.isFinite(n) || !Number.isInteger(n)) throw new Error("视频质量参数必须是整数");
        video_crf = n;
      }
      const primary_font_scale_percent = Number(primaryFontScalePercentText.trim() || "100");
      if (!Number.isFinite(primary_font_scale_percent) || !Number.isInteger(primary_font_scale_percent)) {
        throw new Error("主字幕字号必须是整数百分比");
      }
      if (primary_font_scale_percent < 25 || primary_font_scale_percent > 300) {
        throw new Error("主字幕字号百分比必须在 25~300 之间");
      }
      const secondary_font_scale_percent = Number(secondaryFontScalePercentText.trim() || "100");
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
          auto_publish_platforms: availableAutoPublishPlatforms,
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
  }

  async function restoreDefaults() {
    const ok = await confirm({
      title: "恢复默认配置",
      message: "恢复默认只修改当前表单，仍需点击保存才会写入后端。",
      confirmLabel: "恢复默认",
      tone: "warning",
    });
    if (!ok) return;
    setFormats({ srt: true, ass: true });
    setBurnIn(true);
    setSoftSub(false);
    setAssStyle("clean_white");
    setVideoCodec("av1");
    setUseIntelGpu(false);
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
    setAutoPublishPlatforms([]);
    setPublishTypeidMode("ai_summary");
    setPublishTranslateTitle(true);
    setPublishTitlePrefix("【熟肉】");
    setPublishUseYouTubeCover(true);
    setPublishEnableReprint(true);
  }

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
      <div className="px-1">
        <h2 className="text-xl font-semibold tracking-tight text-slate-950">自动模式</h2>
        <div className="mt-1 text-sm text-slate-600">用于 “YouTube 自动模式” 的默认参数（下载→字幕/翻译→烧录→投稿）。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-semibold">Subtitle</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>

        <div className="mt-3 grid gap-3 lg:grid-cols-2">
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
                <div className="mb-1 text-xs text-slate-600">视频编码</div>
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
                <div className="mb-1 text-xs text-slate-600">
                  {useIntelGpu ? "视频质量（Intel QP / global_quality）" : "视频质量（CRF，可留空使用默认值）"}
                </div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoCrfText}
                  onChange={(e) => setVideoCrfText(e.target.value)}
                  placeholder={
                    useIntelGpu
                      ? videoCodec === "h264"
                        ? "默认 23（Intel h264 CQP）"
                        : "默认 24（Intel av1 global_quality）"
                      : videoCodec === "h264"
                        ? "默认 18（h264 CRF）"
                        : "默认 24（av1 CRF）"
                  }
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">
                {useIntelGpu
                  ? "提示：Intel GPU 下该值是 CQP / global_quality，不是 CRF；越小质量越高、文件通常越大。"
                  : "提示：软件编码下使用 CRF；越小质量越高、体积越大、编码通常越慢。常用范围：h264 18~28；av1 24~35。"}
              </div>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">编码预设（可选）</div>
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
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
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
                <div className="mb-1 text-xs text-slate-600">字幕样式</div>
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
            <div className="mt-3 grid gap-2 lg:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">目标语言</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">翻译 Provider</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateProvider} onChange={(e) => setTranslateProvider(e.target.value)}>
                  <option value="mock">mock</option>
                  <option value="noop">noop</option>
                  <option value="openai">openai</option>
                </select>
              </label>

              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs text-slate-600">翻译风格</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateStyle} onChange={(e) => setTranslateStyle(e.target.value)}>
                  <option value="口语自然">口语自然</option>
                  <option value="正式严谨">正式严谨</option>
                  <option value="电商营销">电商营销</option>
                </select>
              </label>

              <label className="flex items-center gap-2 text-sm lg:col-span-2">
                <input
                  type="checkbox"
                  checked={translateEnableSummary}
                  onChange={(e) => setTranslateEnableSummary(e.target.checked)}
                  disabled={translateProvider !== "openai"}
                />
                启用动态摘要上下文（仅 OpenAI）
              </label>

              <div className="lg:col-span-2 text-xs text-slate-500">
                {youtubeSubtitleMode === "off"
                  ? "逻辑：不复用 YouTube 字幕，直接进入 ASR；若启用翻译，则在 ASR 结果上继续翻译。"
                  : youtubeSubtitleMode === "auto_source"
                    ? "逻辑：优先抓取 YouTube 自动生成的原语言字幕；若启用翻译，则直接进入翻译管线；如果没有可用自动字幕，再回退到 ASR。"
                    : "逻辑：优先找 `target_lang` 对应的 YouTube 字幕；命中后直接复用并跳过翻译；如果没有可用目标字幕，再回退到 ASR。"}
              </div>

              {translateEnabled && translateProvider === "openai" && openaiKeySet === false ? (
                <div className="lg:col-span-2 text-xs text-rose-700">
                  OpenAI API Key 未设置，请先到 <Link className="underline" to="/settings/translate">翻译 / RAG 设置</Link> 保存配置。
                </div>
              ) : null}
            </div>
          </div>

          <div className="rounded border p-3 lg:col-span-2">
            <div className="text-xs text-slate-500">ASR（语音识别）</div>
            <div className="mt-3 grid gap-2 lg:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">ASR 引擎</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrEngine} onChange={(e) => setAsrEngine(e.target.value)}>
                  <option value="auto">auto（使用后端默认）</option>
                  <option value="mock">mock</option>
                  <option value="faster-whisper">faster-whisper</option>
                  <option value="openvino">openvino（方案2 / Intel Arc）</option>
                  <option value="external-whisper">在线 Whisper（自建 faster-whisper）</option>
                  <option value="groq-whisper">groq-whisper（GroqCloud，自动切片）</option>
                  <option value="cloudflare-workers-ai">cloudflare-workers-ai（原生时间轴）</option>
                </select>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">识别语言</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={asrLanguage} onChange={(e) => setAsrLanguage(e.target.value)} placeholder="auto / zh / en ..." />
              </label>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs text-slate-600">ASR 模型（可选）</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrModel} onChange={(e) => setAsrModel(e.target.value)}>
                  <option value="">(use default)</option>
                  {asrEngine === "cloudflare-workers-ai" ? (
                    <option value="@cf/openai/whisper-large-v3-turbo">@cf/openai/whisper-large-v3-turbo</option>
                  ) : null}
                  {asrEngine === "groq-whisper" ? (
                    <>
                      <option value="whisper-large-v3-turbo">whisper-large-v3-turbo</option>
                      <option value="whisper-large-v3">whisper-large-v3</option>
                    </>
                  ) : null}
                  {(whisperModels ?? []).map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
                <div className="mt-2 text-xs text-slate-500">
                  提示：留空表示使用 ASR 设置中的默认模型；选择在线 Whisper 时使用 ASR 设置中保存的服务地址，其他远程引擎同样使用各自的对应配置。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-4 rounded border p-3">
          <div className="text-xs font-semibold text-slate-700">投稿（多平台）</div>
          {enabledPlatforms.length === 0 && (
            <div className="mt-1 text-xs text-amber-600">未启用任何投稿平台，请先到投稿设置中勾选</div>
          )}
          <div className="mt-2 grid gap-2 lg:grid-cols-2">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={autoPublish} onChange={(e) => setAutoPublish(e.target.checked)} />
              自动投稿
            </label>
            <div className="lg:col-span-2">
              <div className="mb-2 text-xs text-slate-600">自动投稿通道</div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {PUBLISH_PLATFORMS.map(({ id, label }) => {
                  const globallyEnabled = enabledPlatforms.includes(id);
                  return (
                    <label key={id} className={`flex items-center gap-2 text-sm ${globallyEnabled ? "" : "text-slate-400"}`}>
                      <input
                        type="checkbox"
                        checked={autoPublishPlatforms.includes(id)}
                        disabled={!globallyEnabled}
                        onChange={(event) => {
                          setAutoPublishPlatforms((current) =>
                            event.target.checked
                              ? [...current.filter((platform) => platform !== id), id]
                              : current.filter((platform) => platform !== id),
                          );
                        }}
                      />
                      {label}{globallyEnabled ? "" : "（未启用）"}
                    </label>
                  );
                })}
              </div>
              <div className="mt-2 text-xs text-slate-500">
                只有此处勾选且在 <Link className="underline" to="/settings/publish">投稿设置</Link> 中启用的通道，才会接收自动模式投稿。
              </div>
              {autoPublish && autoPublishPlatforms.filter((platform) => enabledPlatforms.includes(platform)).length === 0 ? (
                <div className="mt-1 text-xs text-amber-600">当前未选择可用通道，自动模式将跳过投稿。</div>
              ) : null}
            </div>
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
            <label className="block lg:col-span-2">
              <div className="mb-1 text-xs text-slate-600">投稿分区策略</div>
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
                  提示：AI 分区需要启用 OpenAI summary，并在 翻译 / RAG 设置 保存 OpenAI API Key；否则会回退到 B 站预测/手动分区。
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
            投稿前 AI 审核规则请到 <Link className="underline" to="/settings/review">审核设置</Link> 配置。
          </div>
        </div>

      </div>

      <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-xs text-slate-600">
        <div className="font-semibold text-slate-700">提示</div>
        <div className="mt-2">
          - OpenAI 相关参数（base_url/model/timeout/api_key）在 <Link className="underline" to="/settings/translate">翻译 / RAG 设置</Link> 配置。
        </div>
        <div className="mt-1">
          - ASR 默认模型在 <Link className="underline" to="/settings/asr">ASR 设置</Link> 配置。
        </div>
      </div>
      <SettingsSaveBar
        dirty={isDirty}
        busy={busy}
        onSave={saveProfile}
        onDiscard={() => void refresh()}
        extraActions={
          <Button tone="warning" disabled={busy} onClick={() => void restoreDefaults()}>
            恢复默认
          </Button>
        }
      />
    </div>
  );
}
