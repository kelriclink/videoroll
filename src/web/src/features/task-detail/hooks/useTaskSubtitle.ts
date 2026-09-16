import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { useToast } from "../../../components/feedbackContext";
import { subtitleApi } from "../../../api/subtitle";
import type { Asset, SubtitleJob, Task } from "../../../lib/types";
import type { YouTubeSubtitleMode } from "../types";
import { normalizeYouTubeSubtitleMode } from "../utils";

type UseTaskSubtitleArgs = {
  taskId: string | undefined;
  task: Task | null;
  rawAsset: Asset | null;
  isYouTubeTask: boolean;
  subtitleJobs: SubtitleJob[] | null;
  refresh: (opts?: { silent?: boolean }) => Promise<void>;
  loadLogs: (opts?: { silent?: boolean }) => Promise<void>;
  downloadYouTubeSource: (opts?: { showProgress?: boolean }) => Promise<void>;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusy: Dispatch<SetStateAction<boolean>>;
  setPublishTypeidMode: Dispatch<SetStateAction<string>>;
};

export function useTaskSubtitle({
  taskId,
  task,
  rawAsset,
  isYouTubeTask,
  subtitleJobs,
  refresh,
  loadLogs,
  downloadYouTubeSource,
  setError,
  setBusy,
  setPublishTypeidMode,
}: UseTaskSubtitleArgs) {
  const toast = useToast();
  const [subtitleFormats, setSubtitleFormats] = useState({ srt: true, ass: false });
  const [burnIn, setBurnIn] = useState(false);
  const [softSub, setSoftSub] = useState(false);
  const [videoCodec, setVideoCodec] = useState("av1");
  const [useIntelGpu, setUseIntelGpu] = useState(false);
  const [videoPresetText, setVideoPresetText] = useState("");
  const [videoCrfText, setVideoCrfText] = useState("");
  const [asrEngine, setAsrEngine] = useState("auto");
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrModel, setAsrModel] = useState("");
  const [whisperModels, setWhisperModels] = useState<Array<{ name: string; path: string }> | null>(null);
  const [youtubeSubtitleMode, setYouTubeSubtitleMode] = useState<YouTubeSubtitleMode>("target");
  const [translateEnabled, setTranslateEnabled] = useState(false);
  const [bilingual, setBilingual] = useState(false);
  const [targetLang, setTargetLang] = useState("zh");
  const [translateProvider, setTranslateProvider] = useState("mock");
  const [translateStyle, setTranslateStyle] = useState("口语自然");
  const [translateEnableSummary, setTranslateEnableSummary] = useState(true);
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);

  useEffect(() => {
    void subtitleApi.models()
      .then((models) => setWhisperModels(models))
      .catch(() => setWhisperModels(null));
  }, []);

  useEffect(() => {
    if (!taskId) return;
    void (async () => {
      try {
        const [profile, translateSettings] = await Promise.all([
          subtitleApi.autoProfile(),
          subtitleApi.translationSettings(),
        ]);
        const formats = Array.isArray(profile.formats) ? profile.formats : [];
        setSubtitleFormats({ srt: formats.includes("srt"), ass: formats.includes("ass") });
        setBurnIn(Boolean(profile.burn_in));
        setSoftSub(Boolean(profile.soft_sub));
        setVideoCodec((profile.video_codec || "av1").toLowerCase());
        setUseIntelGpu(Boolean(profile.use_intel_gpu));
        setVideoPresetText(typeof profile.video_preset === "string" ? profile.video_preset : "");
        setVideoCrfText(typeof profile.video_crf === "number" ? String(profile.video_crf) : "");
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
        setPublishTypeidMode((profile.publish_typeid_mode || "ai_summary").toLowerCase());
        setOpenaiKeySet(Boolean(translateSettings.openai_api_key_set));
      } catch {
        try {
          const settings = await subtitleApi.translationSettings();
          setOpenaiKeySet(Boolean(settings.openai_api_key_set));
        } catch {}
      }
    })();
  }, [taskId, setPublishTypeidMode]);

  async function submitSubtitleJob(opts: { resume: boolean }) {
    if (!taskId) return;
    if (task?.status === "PUBLISHED") {
      setError("任务已发布；如需重新生成字幕，请创建新任务。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (!rawAsset && isYouTubeTask) await downloadYouTubeSource({ showProgress: false });

      let response: Awaited<ReturnType<typeof subtitleApi.resume>>;
      if (opts.resume) {
        response = await subtitleApi.resume(taskId);
      } else {
        const formats = [subtitleFormats.srt ? "srt" : null, subtitleFormats.ass ? "ass" : null].filter(
          (format): format is string => Boolean(format),
        );
        const crfRaw = videoCrfText.trim();
        let video_crf: number | null = null;
        if (crfRaw) {
          const value = Number(crfRaw);
          if (!Number.isFinite(value) || !Number.isInteger(value)) throw new Error("video_crf 必须是整数");
          video_crf = value;
        }
        const presetRaw = videoPresetText.trim();
        response = await subtitleApi.submit(taskId, {
            formats,
            resume: false,
            burn_in: burnIn,
            soft_sub: softSub,
            ass_style: "clean_white",
            video_codec: videoCodec,
            use_intel_gpu: useIntelGpu,
            video_preset: presetRaw ? presetRaw : null,
            video_crf,
            asr_engine: asrEngine,
            asr_language: asrLanguage,
            asr_model: asrModel.trim() ? asrModel.trim() : null,
            prefer_youtube_subtitles: youtubeSubtitleMode !== "off",
            youtube_subtitle_mode: youtubeSubtitleMode,
            translate_enabled: translateEnabled,
            translate_provider: translateProvider,
            target_lang: targetLang,
            translate_style: translateStyle,
            translate_enable_summary: translateProvider === "openai" ? translateEnableSummary : null,
            bilingual,
        });
      }
      await refresh();
      toast({ kind: "success", title: opts.resume ? "已继续字幕任务" : "已提交字幕任务", message: response.job_id });
      return response;
    } catch (error: unknown) {
      try {
        await refresh({ silent: true });
        await loadLogs({ silent: true });
      } catch {}
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  const canResumeSubtitle = (subtitleJobs ?? []).some((job) => job.status === "failed") || task?.status === "FAILED";

  return {
    subtitleFormats,
    setSubtitleFormats,
    burnIn,
    setBurnIn,
    softSub,
    setSoftSub,
    videoCodec,
    setVideoCodec,
    useIntelGpu,
    setUseIntelGpu,
    videoPresetText,
    setVideoPresetText,
    videoCrfText,
    setVideoCrfText,
    asrEngine,
    setAsrEngine,
    asrLanguage,
    setAsrLanguage,
    asrModel,
    setAsrModel,
    whisperModels,
    youtubeSubtitleMode,
    setYouTubeSubtitleMode,
    translateEnabled,
    setTranslateEnabled,
    bilingual,
    setBilingual,
    targetLang,
    setTargetLang,
    translateProvider,
    setTranslateProvider,
    translateStyle,
    setTranslateStyle,
    translateEnableSummary,
    setTranslateEnableSummary,
    openaiKeySet,
    submitSubtitleJob,
    canResumeSubtitle,
  };
}
