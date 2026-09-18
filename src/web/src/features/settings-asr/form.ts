import { useCallback, useReducer } from "react";

export type ASRDefaults = {
  default_engine: string;
  default_language: string;
  default_model: string;
  openvino_device: string;
  openvino_num_beams: number;
  openvino_max_new_tokens: number;
  openvino_vad_enabled: boolean;
  openvino_vad_threshold: number;
  model_download_proxy?: string;
  external_whisper_base_url: string;
  external_whisper_model: string;
  external_whisper_api_key_set: boolean;
  groq_whisper_model: string;
  groq_whisper_api_key_set: boolean;
  cloudflare_workers_ai_account_id: string;
  cloudflare_workers_ai_model: string;
  cloudflare_workers_ai_api_key_set: boolean;
};

export type ASRFormState = {
  downloadModel: string;
  downloadEngine: string;
  downloadName: string;
  downloadRevision: string;
  downloadForce: boolean;
  uploadName: string;
  defaultEngine: string;
  defaultLanguage: string;
  defaultModel: string;
  openvinoDevice: string;
  openvinoNumBeams: string;
  openvinoMaxNewTokens: string;
  openvinoVadEnabled: boolean;
  openvinoVadThreshold: string;
  modelDownloadProxy: string;
  externalWhisperBaseUrl: string;
  externalWhisperModel: string;
  externalWhisperApiKey: string;
  groqWhisperModel: string;
  groqWhisperApiKey: string;
  cloudflareAccountId: string;
  cloudflareModel: string;
  cloudflareApiKey: string;
};

export const initialASRForm: ASRFormState = {
  downloadModel: "tiny",
  downloadEngine: "faster-whisper",
  downloadName: "",
  downloadRevision: "",
  downloadForce: false,
  uploadName: "",
  defaultEngine: "faster-whisper",
  defaultLanguage: "auto",
  defaultModel: "",
  openvinoDevice: "GPU",
  openvinoNumBeams: "1",
  openvinoMaxNewTokens: "448",
  openvinoVadEnabled: true,
  openvinoVadThreshold: "0.5",
  modelDownloadProxy: "",
  externalWhisperBaseUrl: "",
  externalWhisperModel: "whisper-1",
  externalWhisperApiKey: "",
  groqWhisperModel: "whisper-large-v3-turbo",
  groqWhisperApiKey: "",
  cloudflareAccountId: "",
  cloudflareModel: "@cf/openai/whisper-large-v3-turbo",
  cloudflareApiKey: "",
};

type ASRFormAction =
  | { type: "field"; field: keyof ASRFormState; value: ASRFormState[keyof ASRFormState] }
  | { type: "defaults"; defaults: ASRDefaults };

export function asrFormReducer(state: ASRFormState, action: ASRFormAction): ASRFormState {
  if (action.type === "field") {
    return { ...state, [action.field]: action.value };
  }

  const defaults = action.defaults;
  return {
    ...state,
    defaultEngine: defaults.default_engine || state.defaultEngine,
    defaultLanguage: defaults.default_language || state.defaultLanguage,
    defaultModel: typeof defaults.default_model === "string" ? defaults.default_model : state.defaultModel,
    openvinoDevice: defaults.openvino_device?.trim() || state.openvinoDevice,
    openvinoNumBeams:
      typeof defaults.openvino_num_beams === "number" && defaults.openvino_num_beams > 0
        ? String(defaults.openvino_num_beams)
        : state.openvinoNumBeams,
    openvinoMaxNewTokens:
      typeof defaults.openvino_max_new_tokens === "number" && defaults.openvino_max_new_tokens > 0
        ? String(defaults.openvino_max_new_tokens)
        : state.openvinoMaxNewTokens,
    openvinoVadEnabled:
      typeof defaults.openvino_vad_enabled === "boolean" ? defaults.openvino_vad_enabled : state.openvinoVadEnabled,
    openvinoVadThreshold:
      typeof defaults.openvino_vad_threshold === "number"
        ? String(defaults.openvino_vad_threshold)
        : state.openvinoVadThreshold,
    modelDownloadProxy:
      typeof defaults.model_download_proxy === "string" ? defaults.model_download_proxy : state.modelDownloadProxy,
    externalWhisperBaseUrl:
      typeof defaults.external_whisper_base_url === "string"
        ? defaults.external_whisper_base_url
        : state.externalWhisperBaseUrl,
    externalWhisperModel:
      defaults.external_whisper_model?.trim() || state.externalWhisperModel,
    groqWhisperModel: defaults.groq_whisper_model?.trim() || state.groqWhisperModel,
    cloudflareAccountId:
      typeof defaults.cloudflare_workers_ai_account_id === "string"
        ? defaults.cloudflare_workers_ai_account_id
        : state.cloudflareAccountId,
    cloudflareModel: defaults.cloudflare_workers_ai_model?.trim() || state.cloudflareModel,
  };
}

export function useASRForm() {
  const [state, dispatch] = useReducer(asrFormReducer, initialASRForm);
  const applyDefaults = useCallback(
    (defaults: ASRDefaults) => dispatch({ type: "defaults", defaults }),
    [],
  );
  const setField = <K extends keyof ASRFormState>(field: K) => (value: ASRFormState[K]) => {
    dispatch({ type: "field", field, value });
  };

  return {
    ...state,
    setDownloadModel: setField("downloadModel"),
    setDownloadEngine: setField("downloadEngine"),
    setDownloadName: setField("downloadName"),
    setDownloadRevision: setField("downloadRevision"),
    setDownloadForce: setField("downloadForce"),
    setUploadName: setField("uploadName"),
    setDefaultEngine: setField("defaultEngine"),
    setDefaultLanguage: setField("defaultLanguage"),
    setDefaultModel: setField("defaultModel"),
    setOpenvinoDevice: setField("openvinoDevice"),
    setOpenvinoNumBeams: setField("openvinoNumBeams"),
    setOpenvinoMaxNewTokens: setField("openvinoMaxNewTokens"),
    setOpenvinoVadEnabled: setField("openvinoVadEnabled"),
    setOpenvinoVadThreshold: setField("openvinoVadThreshold"),
    setModelDownloadProxy: setField("modelDownloadProxy"),
    setExternalWhisperBaseUrl: setField("externalWhisperBaseUrl"),
    setExternalWhisperModel: setField("externalWhisperModel"),
    setExternalWhisperApiKey: setField("externalWhisperApiKey"),
    setGroqWhisperModel: setField("groqWhisperModel"),
    setGroqWhisperApiKey: setField("groqWhisperApiKey"),
    setCloudflareAccountId: setField("cloudflareAccountId"),
    setCloudflareModel: setField("cloudflareModel"),
    setCloudflareApiKey: setField("cloudflareApiKey"),
    applyDefaults,
  };
}
