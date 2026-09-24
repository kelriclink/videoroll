import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type YouTubeSubtitleMode = "off" | "target" | "auto_source";
export type SubtitleActionResponse = { job_id: string; status: string };
export type SubtitleAutoProfile = {
  formats: string[];
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  video_codec: string;
  use_intel_gpu: boolean;
  video_preset?: string | null;
  video_crf?: number | null;
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
  publish_typeid_mode?: string | null;
  publish_title_prefix: string;
  publish_translate_title: boolean;
  publish_use_youtube_cover: boolean;
  publish_enable_reprint: boolean;
};

export type SubtitleSubmitPayload = {
  formats: string[];
  resume: boolean;
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  video_codec: string;
  use_intel_gpu: boolean;
  video_preset: string | null;
  video_crf: number | null;
  asr_engine: string;
  asr_language: string;
  asr_model: string | null;
  prefer_youtube_subtitles: boolean;
  youtube_subtitle_mode: "off" | "target" | "auto_source";
  translate_enabled: boolean;
  translate_provider: string;
  target_lang: string;
  translate_style: string;
  translate_enable_summary: boolean | null;
  bilingual: boolean;
};

export type TranslationContextCharacter = {
  name: string;
  target_name: string;
  aliases?: string[];
  role?: string;
  notes?: string;
};

export type TranslationContextTerm = {
  source: string;
  target: string;
  meaning?: string;
};

export type TranslationContextAmbiguity = {
  term: string;
  resolution: string;
};

export type TranslationContextMemory = {
  version?: number;
  topic: string;
  style_notes: string;
  characters: TranslationContextCharacter[];
  terminology: TranslationContextTerm[];
  ambiguities: TranslationContextAmbiguity[];
  recent_scene?: { scene_id: number; summary: string };
};

export type TranslationContextResponse = {
  available: boolean;
  job_id: string;
  key?: string;
  summary: string;
  memory: TranslationContextMemory;
  changed_terms?: string[];
  affected_indices: number[];
};

export type SubtitleQualityReport = {
  version: number;
  score: number;
  segment_count: number;
  source_segment_count: number;
  translation_segment_count: number;
  metrics: Record<string, number>;
  adaptive_profile: {
    readability: Record<string, number>;
    scene: Record<string, number>;
  };
  context: {
    characters: number;
    terminology: number;
    ambiguities: number;
  };
  issues: Array<Record<string, unknown>>;
};

export type SubtitleQualityResponse = {
  available: boolean;
  job_id: string;
  key?: string;
  report: SubtitleQualityReport | null;
};

export const subtitleApi = {
  models() {
    return fetchJson<Array<{ name: string; path: string }>>(orchestratorUrl("/subtitle/models"));
  },

  autoProfile() {
    return fetchJson<SubtitleAutoProfile>(orchestratorUrl("/subtitle/auto/profile"));
  },

  translationSettings() {
    return fetchJson<{ openai_api_key_set: boolean }>(orchestratorUrl("/subtitle/translate/settings"));
  },

  quality(taskId: string) {
    return fetchJson<SubtitleQualityResponse>(orchestratorUrl(`/subtitle/tasks/${taskId}/quality`));
  },

  translationContext(taskId: string) {
    return fetchJson<TranslationContextResponse>(orchestratorUrl(`/subtitle/tasks/${taskId}/translation-context`));
  },

  updateTranslationContext(
    taskId: string,
    payload: { summary: string; memory: TranslationContextMemory },
  ) {
    return fetchJson<TranslationContextResponse>(orchestratorUrl(`/subtitle/tasks/${taskId}/translation-context`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  retranslate(taskId: string, indices: number[]) {
    return fetchJson<SubtitleActionResponse>(orchestratorUrl(`/tasks/${taskId}/actions/subtitle_retranslate`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ indices }),
    });
  },

  submit(taskId: string, payload: SubtitleSubmitPayload) {
    return fetchJson<SubtitleActionResponse>(orchestratorUrl(`/tasks/${taskId}/actions/subtitle`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  resume(taskId: string) {
    return fetchJson<SubtitleActionResponse>(orchestratorUrl(`/tasks/${taskId}/actions/subtitle_resume`), { method: "POST" });
  },
};
