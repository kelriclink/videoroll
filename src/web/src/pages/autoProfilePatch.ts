// Compare the form's normalized snapshots, not raw server values (nulls,
// display defaults, or filtered platform lists). Send only edited fields.
const snapshotKeys: Record<string, string> = {
  formats: "formats",
  burn_in: "burnIn",
  soft_sub: "softSub",
  ass_style: "assStyle",
  video_codec: "videoCodec",
  use_intel_gpu: "useIntelGpu",
  video_preset: "videoPresetText",
  video_crf: "videoCrfText",
  primary_font_scale_percent: "primaryFontScalePercentText",
  secondary_font_scale_percent: "secondaryFontScalePercentText",
  asr_engine: "asrEngine",
  asr_language: "asrLanguage",
  asr_model: "asrModel",
  prefer_youtube_subtitles: "youtubeSubtitleMode",
  youtube_subtitle_mode: "youtubeSubtitleMode",
  translate_enabled: "translateEnabled",
  translate_provider: "translateProvider",
  translate_model_provider_id: "translateModelProviderId",
  translate_model_id: "translateModelId",
  target_lang: "targetLang",
  translate_style: "translateStyle",
  translate_enable_summary: "translateEnableSummary",
  bilingual: "bilingual",
  auto_publish: "autoPublish",
  auto_publish_platforms: "autoPublishPlatforms",
  publish_typeid_mode: "publishTypeidMode",
  publish_title_prefix: "publishTitlePrefix",
  auto_dub_p2: "autoDubP2",
  auto_dub_p2_prefix: "autoDubP2Prefix",
  publish_translate_title: "publishTranslateTitle",
  publish_use_youtube_cover: "publishUseYouTubeCover",
  publish_enable_reprint: "publishEnableReprint",
};

export function autoProfilePatch(
  payload: Record<string, unknown>, saved: string, current: string,
): Record<string, unknown> {
  if (!saved) throw new Error("请先加载自动处理配置");
  const before = JSON.parse(saved) as Record<string, unknown>;
  const after = JSON.parse(current) as Record<string, unknown>;
  const patch: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    const field = snapshotKeys[key];
    if (!field || !(field in before) || !(field in after)) {
      throw new Error(`无法比较自动处理配置字段：${key}`);
    }
    if (JSON.stringify(before[field]) !== JSON.stringify(after[field])) patch[key] = value;
  }
  // A provider change must also carry the selected model, even when its ID
  // is textually identical to the old provider's selection.
  if ("translate_model_provider_id" in patch) {
    patch.translate_model_id = payload.translate_model_id;
  }
  return patch;
}
