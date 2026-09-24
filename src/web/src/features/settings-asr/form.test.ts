import { describe, expect, it } from "vitest";

import { asrFormReducer, initialASRForm, type ASRDefaults } from "./form";

const defaults: ASRDefaults = {
  default_engine: "openvino",
  default_language: "en",
  default_model: "/models/whisper/ov",
  openvino_device: "GPU",
  openvino_num_beams: 3,
  openvino_max_new_tokens: 512,
  openvino_vad_enabled: false,
  openvino_vad_threshold: 0.42,
  model_download_proxy: "http://proxy.example",
  external_whisper_base_url: "https://whisper.example/v1",
  external_whisper_model: "whisper-1",
  external_whisper_api_key_set: true,
  external_whisper_batch_size: 1,
  external_whisper_vad_enabled: true,
  external_whisper_vad_threshold: 0.5,
  external_whisper_min_silence_ms: 2000,
  external_whisper_speech_pad_ms: 400,
  external_whisper_condition_on_previous_text: true,
  external_whisper_max_segment_seconds: 12,
  external_whisper_max_segment_chars: 120,
  groq_whisper_model: "whisper-large-v3-turbo",
  groq_whisper_api_key_set: true,
  cloudflare_workers_ai_account_id: "acct",
  cloudflare_workers_ai_model: "@cf/openai/whisper-large-v3-turbo",
  cloudflare_workers_ai_api_key_set: true,
};

describe("ASR settings form", () => {
  it("maps persisted ASR defaults into editable form fields", () => {
    const next = asrFormReducer(initialASRForm, { type: "defaults", defaults });
    expect(next.defaultEngine).toBe("openvino");
    expect(next.defaultLanguage).toBe("en");
    expect(next.openvinoNumBeams).toBe("3");
    expect(next.openvinoMaxNewTokens).toBe("512");
    expect(next.openvinoVadEnabled).toBe(false);
    expect(next.openvinoVadThreshold).toBe("0.42");
    expect(next.externalWhisperBatchSize).toBe("1");
    expect(next.externalWhisperVadEnabled).toBe(true);
    expect(next.externalWhisperMaxSegmentSeconds).toBe("12");
    expect(next.externalWhisperMaxSegmentChars).toBe("120");
    expect(next.cloudflareAccountId).toBe("acct");
  });

  it("patches one field without losing the rest of the form", () => {
    const next = asrFormReducer(initialASRForm, { type: "field", field: "defaultModel", value: "large-v3" });
    expect(next.defaultModel).toBe("large-v3");
    expect(next.defaultEngine).toBe(initialASRForm.defaultEngine);
  });
});
