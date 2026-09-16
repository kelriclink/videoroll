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
