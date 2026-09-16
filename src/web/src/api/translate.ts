import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type TranslateSettings = {
  default_provider: string;
  default_target_lang: string;
  default_style: string;
  default_batch_size: number;
  default_max_retries: number;
  default_enable_summary: boolean;
  openai_api_key_set: boolean;
  openai_base_url: string;
  openai_model: string;
  openai_temperature: number;
  openai_timeout_seconds: number;
  openai_max_retries: number;
  openai_api_type: "openai" | "cerebras";
  openai_enable_thinking: boolean;
  cerebras_reasoning_effort: "low" | "medium" | "high";
  cerebras_reasoning_format: "parsed" | "raw" | "hidden";
  rag_enabled: boolean;
  rag_top_k: number;
  rag_min_score: number;
  rag_embedding_provider: string;
  rag_embedding_model: string;
  rag_embedding_dimensions: number;
  rag_embedding_model_dir: string;
  rag_embedding_device: string;
  rag_embedding_api_key_set: boolean;
  rag_embedding_base_url: string;
  rag_embedding_timeout_seconds: number;
  rag_auto_discover_terms: boolean;
  rag_auto_learn_terms: boolean;
  rag_dictionary_enabled: boolean;
  rag_dictionary_top_k: number;
  rag_dictionary_min_quality: number;
  rag_dictionary_auto_promote: boolean;
  rag_wiki_enabled: boolean;
  rag_search_enabled: boolean;
  rag_search_url: string;
  rag_search_categories: string;
  rag_search_engines: string;
  rag_search_fallback_engines: string;
  rag_search_language: string;
  rag_search_safesearch: number;
  rag_search_time_range: string;
  rag_search_pageno: number;
  rag_domain: string;
  rag_agent_parallelism: number;
  rag_agent_timeout_seconds: number;
  rag_agent_skills_enabled: boolean;
  rag_agent_builtin_skills_enabled: boolean;
  rag_agent_user_skills_enabled: boolean;
};

export type TranslateSettingsUpdate = Partial<Omit<TranslateSettings, "openai_api_key_set" | "rag_embedding_api_key_set">> & {
  openai_api_key?: string;
  rag_embedding_api_key?: string;
};

export type EmbeddingModelInfo = { name: string; path: string; size_bytes?: number | null };
export type AgentSkillInfo = {
  name: string;
  description: string;
  domain: string[];
  triggers: string[];
  allowed_tools: string[];
  runnable: boolean;
  run_mode: string;
  source: string;
  path: string;
  resource_count: number;
};
export type EmbeddingRebuildResponse = {
  total: number;
  updated: number;
  failed: number;
  skipped: number;
  embedding_model: string;
  dimensions: number;
};
export type EmbeddingTestResponse = {
  provider: string;
  model: string;
  dimensions: number;
  expected_dimensions: number;
  ok: boolean;
};

export const translateApi = {
  settings() {
    return fetchJson<TranslateSettings>(orchestratorUrl("/subtitle/translate/settings"));
  },

  updateSettings(payload: TranslateSettingsUpdate) {
    return fetchJson(orchestratorUrl("/subtitle/translate/settings"), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  embeddingModels(modelDir: string) {
    return fetchJson<EmbeddingModelInfo[]>(orchestratorUrl("/subtitle/embedding/models/list"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_dir: modelDir }),
    });
  },

  agentSkills() {
    return fetchJson<AgentSkillInfo[]>(orchestratorUrl("/subtitle/agent/skills"));
  },

  rebuildEmbeddings(limit = 10000) {
    return fetchJson<EmbeddingRebuildResponse>(orchestratorUrl("/subtitle/knowledge/rebuild-embeddings"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
  },

  downloadEmbeddingModel(payload: { model: string; name: string | null; model_dir: string; force: boolean }) {
    return fetchJson(orchestratorUrl("/subtitle/embedding/models/download"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  testEmbedding(payload: {
    text: string;
    provider: string;
    model: string;
    model_dir: string;
    dimensions: number;
    device: string;
    api_key?: string;
    base_url: string;
    timeout_seconds: number;
  }) {
    return fetchJson<EmbeddingTestResponse>(orchestratorUrl("/subtitle/embedding/test"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  testTranslation(payload: { text: string; target_lang: string; style: string }) {
    return fetchJson<{ translated_text: string }>(orchestratorUrl("/subtitle/translate/test"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
};
