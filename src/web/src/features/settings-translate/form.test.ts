import { describe, expect, it } from "vitest";
import type { TranslateSettings } from "../../api/translate";
import { formToUpdatePayload, initialTranslateForm, settingsToForm } from "./form";

function serverSettings(overrides: Partial<TranslateSettings> = {}): TranslateSettings {
  return {
    default_provider: "openai",
    default_target_lang: "zh",
    default_style: "口语自然",
    default_batch_size: 50,
    default_max_retries: 0,
    default_enable_summary: false,
    openai_api_key_set: true,
    openai_base_url: "https://api.openai.com/v1",
    openai_model: "gpt-4o-mini",
    openai_temperature: 0,
    openai_timeout_seconds: 0,
    openai_max_retries: 0,
    openai_api_type: "cerebras",
    openai_enable_thinking: false,
    cerebras_reasoning_effort: "low",
    cerebras_reasoning_format: "raw",
    rag_enabled: false,
    rag_top_k: 0,
    rag_min_score: 0,
    rag_embedding_provider: "local",
    rag_embedding_model: "",
    rag_embedding_dimensions: 0,
    rag_embedding_model_dir: "",
    rag_embedding_device: "cpu",
    rag_embedding_api_key_set: false,
    rag_embedding_base_url: "",
    rag_embedding_timeout_seconds: 0,
    rag_auto_discover_terms: false,
    rag_auto_learn_terms: false,
    rag_dictionary_enabled: false,
    rag_dictionary_top_k: 0,
    rag_dictionary_min_quality: 0,
    rag_dictionary_auto_promote: false,
    rag_wiki_enabled: false,
    rag_search_enabled: false,
    rag_search_url: "",
    rag_search_categories: "",
    rag_search_engines: "",
    rag_search_fallback_engines: "",
    rag_search_language: "",
    rag_search_safesearch: 0,
    rag_search_time_range: "",
    rag_search_pageno: 0,
    rag_domain: "",
    rag_agent_parallelism: 0,
    rag_agent_timeout_seconds: 0,
    rag_agent_skills_enabled: false,
    rag_agent_builtin_skills_enabled: false,
    rag_agent_user_skills_enabled: false,
    ...overrides,
  };
}

describe("settingsToForm", () => {
  it("preserves meaningful zero, false, and empty values", () => {
    const form = settingsToForm(serverSettings());
    expect(form.translation.maxRetries).toBe(0);
    expect(form.translation.enableSummary).toBe(false);
    expect(form.llm.temperature).toBe(0);
    expect(form.llm.timeoutSeconds).toBe(0);
    expect(form.rag.topK).toBe(0);
    expect(form.rag.dictionary.minQuality).toBe(0);
    expect(form.rag.search.safesearch).toBe(0);
    expect(form.rag.search.pageno).toBe(0);
    expect(form.rag.agent.parallelism).toBe(0);
    expect(form.embedding.model).toBe("");
  });

  it("uses documented defaults only for missing nullable fields", () => {
    const form = settingsToForm(serverSettings({
      default_max_retries: undefined as unknown as number,
      openai_max_retries: undefined as unknown as number,
      rag_dictionary_enabled: undefined as unknown as boolean,
      rag_search_categories: undefined as unknown as string,
      rag_agent_timeout_seconds: undefined as unknown as number,
    }));
    expect(form.translation.maxRetries).toBe(0);
    expect(form.llm.maxRetries).toBe(3);
    expect(form.rag.dictionary.enabled).toBe(true);
    expect(form.rag.search.categories).toBe("general");
    expect(form.rag.agent.timeoutSeconds).toBe(120);
  });
});

describe("formToUpdatePayload", () => {
  it("omits blank secrets and includes newly entered secrets", () => {
    const blank = formToUpdatePayload(initialTranslateForm, { openaiApiKey: " ", embeddingApiKey: "" });
    expect(blank).not.toHaveProperty("openai_api_key");
    expect(blank).not.toHaveProperty("rag_embedding_api_key");

    const withSecrets = formToUpdatePayload(initialTranslateForm, { openaiApiKey: " sk-test ", embeddingApiKey: "embed-test" });
    expect(withSecrets.openai_api_key).toBe("sk-test");
    expect(withSecrets.rag_embedding_api_key).toBe("embed-test");
  });

  it("maps nested RAG, search, agent, and Cerebras fields without dropping values", () => {
    const form = settingsToForm(serverSettings());
    const payload = formToUpdatePayload(form, { openaiApiKey: "", embeddingApiKey: "" });
    expect(payload.openai_api_type).toBe("cerebras");
    expect(payload.cerebras_reasoning_effort).toBe("low");
    expect(payload.cerebras_reasoning_format).toBe("raw");
    expect(payload.rag_search_language).toBe("all");
    expect(payload.rag_search_safesearch).toBe(0);
    expect(payload.rag_agent_parallelism).toBe(0);
    expect(payload.rag_agent_user_skills_enabled).toBe(false);
  });
});
