import type { TranslateSettings, TranslateSettingsUpdate } from "../../api/translate";

export type TranslateSettingsTab = "translation" | "rag" | "embedding" | "test";

export type TranslateFormState = {
  translation: {
    provider: string;
    targetLang: string;
    style: string;
    batchSize: number;
    maxRetries: number;
    enableSummary: boolean;
  };
  llm: {
    baseUrl: string;
    model: string;
    temperature: number;
    timeoutSeconds: number;
    maxRetries: number;
    apiType: "openai" | "cerebras";
    enableThinking: boolean;
    cerebrasReasoningEffort: "low" | "medium" | "high";
    cerebrasReasoningFormat: "parsed" | "raw" | "hidden";
  };
  rag: {
    enabled: boolean;
    topK: number;
    minScore: number;
    autoDiscoverTerms: boolean;
    autoLearnTerms: boolean;
    dictionary: {
      enabled: boolean;
      topK: number;
      minQuality: number;
      autoPromote: boolean;
    };
    wikiEnabled: boolean;
    search: {
      enabled: boolean;
      url: string;
      categories: string;
      engines: string;
      fallbackEngines: string;
      language: string;
      safesearch: number;
      timeRange: string;
      pageno: number;
    };
    domain: string;
    agent: {
      parallelism: number;
      timeoutSeconds: number;
      skillsEnabled: boolean;
      builtinSkillsEnabled: boolean;
      userSkillsEnabled: boolean;
    };
  };
  embedding: {
    provider: string;
    model: string;
    dimensions: number;
    modelDir: string;
    device: string;
    baseUrl: string;
    timeoutSeconds: number;
  };
  test: {
    text: string;
    targetLang: string;
    style: string;
  };
  embeddingDownload: {
    model: string;
    name: string;
  };
};

export type SecretDraft = { openaiApiKey: string; embeddingApiKey: string };

export const initialTranslateForm: TranslateFormState = {
  translation: {
    provider: "openai",
    targetLang: "zh",
    style: "口语自然",
    batchSize: 50,
    maxRetries: 2,
    enableSummary: true,
  },
  llm: {
    baseUrl: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    temperature: 0.2,
    timeoutSeconds: 60,
    maxRetries: 3,
    apiType: "openai",
    enableThinking: false,
    cerebrasReasoningEffort: "medium",
    cerebrasReasoningFormat: "parsed",
  },
  rag: {
    enabled: false,
    topK: 8,
    minScore: 0.68,
    autoDiscoverTerms: false,
    autoLearnTerms: false,
    dictionary: { enabled: true, topK: 8, minQuality: 0, autoPromote: false },
    wikiEnabled: false,
    search: {
      enabled: false,
      url: "",
      categories: "general",
      engines: "",
      fallbackEngines: "bing,baidu",
      language: "all",
      safesearch: 0,
      timeRange: "",
      pageno: 1,
    },
    domain: "",
    agent: { parallelism: 1, timeoutSeconds: 120, skillsEnabled: false, builtinSkillsEnabled: true, userSkillsEnabled: true },
  },
  embedding: {
    provider: "openai",
    model: "text-embedding-3-small",
    dimensions: 1536,
    modelDir: "/models/embeddings",
    device: "cpu",
    baseUrl: "https://api.openai.com/v1",
    timeoutSeconds: 60,
  },
  test: { text: "Hello world. This is a translation test.", targetLang: "zh", style: "口语自然" },
  embeddingDownload: { model: "BAAI/bge-small-zh-v1.5", name: "" },
};

export type TranslateFormAction =
  | { type: "replace"; value: TranslateFormState }
  | { type: "patchTranslation"; patch: Partial<TranslateFormState["translation"]> }
  | { type: "patchLlm"; patch: Partial<TranslateFormState["llm"]> }
  | { type: "patchEmbedding"; patch: Partial<TranslateFormState["embedding"]> }
  | { type: "patchRag"; patch: Partial<TranslateFormState["rag"]> }
  | { type: "patchDictionary"; patch: Partial<TranslateFormState["rag"]["dictionary"]> }
  | { type: "patchSearch"; patch: Partial<TranslateFormState["rag"]["search"]> }
  | { type: "patchAgent"; patch: Partial<TranslateFormState["rag"]["agent"]> }
  | { type: "patchTest"; patch: Partial<TranslateFormState["test"]> }
  | { type: "patchEmbeddingDownload"; patch: Partial<TranslateFormState["embeddingDownload"]> };

export function translateFormReducer(state: TranslateFormState, action: TranslateFormAction): TranslateFormState {
  switch (action.type) {
    case "replace":
      return action.value;
    case "patchTranslation":
      return { ...state, translation: { ...state.translation, ...action.patch } };
    case "patchLlm":
      return { ...state, llm: { ...state.llm, ...action.patch } };
    case "patchEmbedding":
      return { ...state, embedding: { ...state.embedding, ...action.patch } };
    case "patchRag":
      return { ...state, rag: { ...state.rag, ...action.patch } };
    case "patchDictionary":
      return { ...state, rag: { ...state.rag, dictionary: { ...state.rag.dictionary, ...action.patch } } };
    case "patchSearch":
      return { ...state, rag: { ...state.rag, search: { ...state.rag.search, ...action.patch } } };
    case "patchAgent":
      return { ...state, rag: { ...state.rag, agent: { ...state.rag.agent, ...action.patch } } };
    case "patchTest":
      return { ...state, test: { ...state.test, ...action.patch } };
    case "patchEmbeddingDownload":
      return { ...state, embeddingDownload: { ...state.embeddingDownload, ...action.patch } };
  }
}

export function settingsToForm(
  settings: TranslateSettings,
  preserve?: Pick<TranslateFormState, "test" | "embeddingDownload">,
): TranslateFormState {
  return {
    translation: {
      provider: settings.default_provider,
      targetLang: settings.default_target_lang,
      style: settings.default_style,
      batchSize: settings.default_batch_size,
      maxRetries: settings.default_max_retries ?? 0,
      enableSummary: settings.default_enable_summary,
    },
    llm: {
      baseUrl: settings.openai_base_url,
      model: settings.openai_model,
      temperature: settings.openai_temperature,
      timeoutSeconds: settings.openai_timeout_seconds,
      maxRetries: settings.openai_max_retries ?? 3,
      apiType: settings.openai_api_type ?? "openai",
      enableThinking: settings.openai_enable_thinking ?? false,
      cerebrasReasoningEffort: settings.cerebras_reasoning_effort ?? "medium",
      cerebrasReasoningFormat: settings.cerebras_reasoning_format ?? "parsed",
    },
    rag: {
      enabled: settings.rag_enabled,
      topK: settings.rag_top_k,
      minScore: settings.rag_min_score,
      autoDiscoverTerms: settings.rag_auto_discover_terms,
      autoLearnTerms: settings.rag_auto_learn_terms,
      dictionary: {
        enabled: settings.rag_dictionary_enabled ?? true,
        topK: settings.rag_dictionary_top_k ?? 8,
        minQuality: settings.rag_dictionary_min_quality ?? 0,
        autoPromote: settings.rag_dictionary_auto_promote ?? false,
      },
      wikiEnabled: settings.rag_wiki_enabled ?? false,
      search: {
        enabled: settings.rag_search_enabled,
        url: settings.rag_search_url,
        categories: settings.rag_search_categories || "general",
        engines: settings.rag_search_engines || "",
        fallbackEngines: settings.rag_search_fallback_engines || "bing,baidu",
        language: settings.rag_search_language || "all",
        safesearch: settings.rag_search_safesearch ?? 0,
        timeRange: settings.rag_search_time_range || "",
        pageno: settings.rag_search_pageno ?? 1,
      },
      domain: settings.rag_domain,
      agent: {
        parallelism: settings.rag_agent_parallelism ?? 1,
        timeoutSeconds: settings.rag_agent_timeout_seconds ?? 120,
        skillsEnabled: settings.rag_agent_skills_enabled ?? false,
        builtinSkillsEnabled: settings.rag_agent_builtin_skills_enabled ?? true,
        userSkillsEnabled: settings.rag_agent_user_skills_enabled ?? true,
      },
    },
    embedding: {
      provider: settings.rag_embedding_provider,
      model: settings.rag_embedding_model,
      dimensions: settings.rag_embedding_dimensions,
      modelDir: settings.rag_embedding_model_dir,
      device: settings.rag_embedding_device,
      baseUrl: settings.rag_embedding_base_url,
      timeoutSeconds: settings.rag_embedding_timeout_seconds,
    },
    test: {
      ...(preserve?.test ?? initialTranslateForm.test),
      targetLang: settings.default_target_lang || "zh",
      style: settings.default_style || "口语自然",
    },
    embeddingDownload: preserve?.embeddingDownload ?? initialTranslateForm.embeddingDownload,
  };
}

export function formToUpdatePayload(form: TranslateFormState, secrets: SecretDraft): TranslateSettingsUpdate {
  const payload: TranslateSettingsUpdate = {
    default_provider: form.translation.provider,
    default_target_lang: form.translation.targetLang,
    default_style: form.translation.style,
    default_batch_size: form.translation.batchSize,
    default_max_retries: form.translation.maxRetries,
    default_enable_summary: form.translation.enableSummary,
    openai_base_url: form.llm.baseUrl,
    openai_model: form.llm.model,
    openai_temperature: form.llm.temperature,
    openai_timeout_seconds: form.llm.timeoutSeconds,
    openai_max_retries: form.llm.maxRetries,
    openai_api_type: form.llm.apiType,
    openai_enable_thinking: form.llm.enableThinking,
    cerebras_reasoning_effort: form.llm.cerebrasReasoningEffort,
    cerebras_reasoning_format: form.llm.cerebrasReasoningFormat,
    rag_enabled: form.rag.enabled,
    rag_top_k: form.rag.topK,
    rag_min_score: form.rag.minScore,
    rag_embedding_provider: form.embedding.provider,
    rag_embedding_model: form.embedding.model,
    rag_embedding_dimensions: form.embedding.dimensions,
    rag_embedding_model_dir: form.embedding.modelDir,
    rag_embedding_device: form.embedding.device,
    rag_embedding_base_url: form.embedding.baseUrl,
    rag_embedding_timeout_seconds: form.embedding.timeoutSeconds,
    rag_auto_discover_terms: form.rag.autoDiscoverTerms,
    rag_auto_learn_terms: form.rag.autoLearnTerms,
    rag_dictionary_enabled: form.rag.dictionary.enabled,
    rag_dictionary_top_k: form.rag.dictionary.topK,
    rag_dictionary_min_quality: form.rag.dictionary.minQuality,
    rag_dictionary_auto_promote: form.rag.dictionary.autoPromote,
    rag_wiki_enabled: form.rag.wikiEnabled,
    rag_search_enabled: form.rag.search.enabled,
    rag_search_url: form.rag.search.url,
    rag_search_categories: form.rag.search.categories,
    rag_search_engines: form.rag.search.engines,
    rag_search_fallback_engines: form.rag.search.fallbackEngines,
    rag_search_language: form.rag.search.language,
    rag_search_safesearch: form.rag.search.safesearch,
    rag_search_time_range: form.rag.search.timeRange,
    rag_search_pageno: form.rag.search.pageno,
    rag_domain: form.rag.domain,
    rag_agent_parallelism: form.rag.agent.parallelism,
    rag_agent_timeout_seconds: form.rag.agent.timeoutSeconds,
    rag_agent_skills_enabled: form.rag.agent.skillsEnabled,
    rag_agent_builtin_skills_enabled: form.rag.agent.builtinSkillsEnabled,
    rag_agent_user_skills_enabled: form.rag.agent.userSkillsEnabled,
  };
  if (secrets.openaiApiKey.trim()) payload.openai_api_key = secrets.openaiApiKey.trim();
  if (secrets.embeddingApiKey.trim()) payload.rag_embedding_api_key = secrets.embeddingApiKey.trim();
  return payload;
}

export function csvItems(value: string): string[] {
  const seen = new Set<string>();
  const output: string[] = [];
  value.split(",").map((item) => item.trim()).filter(Boolean).forEach((item) => {
    const key = item.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    output.push(item);
  });
  return output;
}

export function toggleCsvItem(value: string, item: string): string {
  const clean = item.trim();
  const current = csvItems(value);
  return current.some((entry) => entry.toLowerCase() === clean.toLowerCase())
    ? current.filter((entry) => entry.toLowerCase() !== clean.toLowerCase()).join(",")
    : [...current, clean].join(",");
}

export function safeEmbeddingModelName(raw: string): string {
  const value = raw.trim().replace(/[\\/]/g, "--").replace(/[^A-Za-z0-9._-]/g, "-");
  return value.slice(0, 96) || "embedding-model";
}

export const SEARXNG_CATEGORY_PRESETS = ["general", "it", "science", "news", "videos", "images", "files", "social media"];
export const SEARXNG_ENGINE_PRESETS = [
  "bing", "baidu", "brave", "duckduckgo", "google", "startpage", "qwant", "mojeek", "wikipedia", "wikidata", "arxiv", "crossref", "pubmed", "semantic scholar", "github", "stackoverflow", "reddit", "youtube",
];
