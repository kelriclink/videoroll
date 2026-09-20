import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useConfirm } from "../../components/feedbackContext";
import { useUnsavedChangesGuard } from "../../hooks/useUnsavedChangesGuard";
import {
  translateApi,
  type AgentSkillInfo,
  type EmbeddingModelInfo,
  type EmbeddingRuntimeStatus,
  type TranslateSettings,
} from "../../api/translate";
import {
  formToUpdatePayload,
  initialTranslateForm,
  settingsToForm,
  translateFormReducer,
  type SecretDraft,
  type TranslateFormAction,
  type TranslateSettingsTab,
} from "./form";

type ResultState = { translation: string | null; embedding: string | null; rebuild: string | null };

function persistedFormSnapshot(form: typeof initialTranslateForm): string {
  return JSON.stringify({
    translation: form.translation,
    llm: form.llm,
    rag: form.rag,
    embedding: form.embedding,
  });
}

export function useSettingsTranslateController() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<TranslateSettings | null>(null);
  const [embeddingModels, setEmbeddingModels] = useState<EmbeddingModelInfo[]>([]);
  const [embeddingRuntime, setEmbeddingRuntime] = useState<EmbeddingRuntimeStatus | null>(null);
  const [agentSkills, setAgentSkills] = useState<AgentSkillInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeTab, setActiveTab] = useState<TranslateSettingsTab>("translation");
  const [form, dispatch] = useReducer(translateFormReducer, initialTranslateForm);
  const [secrets, setSecrets] = useState<SecretDraft>({ openaiApiKey: "", embeddingApiKey: "" });
  const [results, setResults] = useState<ResultState>({ translation: null, embedding: null, rebuild: null });
  const formRef = useRef(form);
  const savedFormSnapshotRef = useRef("");
  formRef.current = form;

  const update = useCallback((type: TranslateFormAction["type"], patch: Record<string, unknown>) => {
    dispatch({ type, patch } as TranslateFormAction);
  }, []);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const nextSettings = await translateApi.settings();
      const [localModels, skills, runtime] = await Promise.all([
        translateApi.embeddingModels(nextSettings.rag_embedding_model_dir).catch(() => []),
        translateApi.agentSkills().catch(() => []),
        translateApi.embeddingRuntime().catch(() => null),
      ]);
      const nextForm = settingsToForm(nextSettings, {
        test: formRef.current.test,
        embeddingDownload: formRef.current.embeddingDownload,
      });
      setSettings(nextSettings);
      savedFormSnapshotRef.current = persistedFormSnapshot(nextForm);
      dispatch({
        type: "replace",
        value: nextForm,
      });
      setEmbeddingModels(localModels);
      setAgentSkills(skills);
      setEmbeddingRuntime(runtime);
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const isDirty =
    Boolean(settings) &&
    (
      savedFormSnapshotRef.current !== persistedFormSnapshot(form) ||
      Boolean(secrets.openaiApiKey.trim()) ||
      Boolean(secrets.embeddingApiKey.trim())
    );

  useUnsavedChangesGuard(isDirty, {
    message: "离开当前页面会丢失尚未保存的翻译 / RAG 配置。",
  });

  function discardChanges() {
    if (!settings) return;
    dispatch({
      type: "replace",
      value: settingsToForm(settings, {
        test: formRef.current.test,
        embeddingDownload: formRef.current.embeddingDownload,
      }),
    });
    setSecrets({ openaiApiKey: "", embeddingApiKey: "" });
    setError(null);
  }

  const persistSettings = useCallback(async () => {
    await translateApi.updateSettings(formToUpdatePayload(form, secrets));
  }, [form, secrets]);

  async function saveSettings() {
    setBusy(true);
    setError(null);
    try {
      await persistSettings();
      setSecrets({ openaiApiKey: "", embeddingApiKey: "" });
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function rebuildKnowledgeEmbeddings() {
    const ok = await confirm({
      title: "重建知识库向量",
      message: "会使用当前页面里的 embedding 配置重新生成知识库向量。知识条目较多时会比较慢。",
      confirmLabel: "开始重建",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    setResults((current) => ({ ...current, rebuild: null }));
    try {
      await persistSettings();
      setSecrets({ openaiApiKey: "", embeddingApiKey: "" });
      const response = await translateApi.rebuildEmbeddings();
      setResults((current) => ({
        ...current,
        rebuild: `${response.embedding_model} / ${response.dimensions} dims：共 ${response.total} 条，更新 ${response.updated}，跳过 ${response.skipped}，失败 ${response.failed}`,
      }));
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function downloadEmbeddingModel() {
    setBusy(true);
    setError(null);
    try {
      await translateApi.downloadEmbeddingModel({
        model: form.embeddingDownload.model,
        name: form.embeddingDownload.name.trim() || null,
        model_dir: form.embedding.modelDir,
        force: false,
      });
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function testEmbedding() {
    setBusy(true);
    setError(null);
    setResults((current) => ({ ...current, embedding: null }));
    try {
      const response = await translateApi.testEmbedding({
        text: "Rush B with an AWP",
        provider: form.embedding.provider,
        model: form.embedding.model,
        model_dir: form.embedding.modelDir,
        dimensions: form.embedding.dimensions,
        device: form.embedding.device,
        api_key: secrets.embeddingApiKey.trim() || undefined,
        base_url: form.embedding.baseUrl,
        timeout_seconds: form.embedding.timeoutSeconds,
      });
      setResults((current) => ({
        ...current,
        embedding: `${response.provider}:${response.model} -> ${response.dimensions} dims${response.ok ? "" : `，期望 ${response.expected_dimensions}`}`,
      }));
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function testTranslation() {
    setBusy(true);
    setError(null);
    setResults((current) => ({ ...current, translation: null }));
    try {
      const response = await translateApi.testTranslation({
        text: form.test.text,
        target_lang: form.test.targetLang,
        style: form.test.style,
      });
      setResults((current) => ({ ...current, translation: response.translated_text }));
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function clearOpenAiKey() {
    const ok = await confirm({ title: "清除 OpenAI API Key", message: "确定清除 OpenAI API Key 吗？", confirmLabel: "清除", tone: "danger" });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await translateApi.updateSettings({ openai_api_key: "" });
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function clearEmbeddingKey() {
    const ok = await confirm({ title: "清除 Embedding API Key", message: "确定清除 OpenAI 兼容 embedding 的 API Key 吗？", confirmLabel: "清除", tone: "danger" });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await translateApi.updateSettings({ rag_embedding_api_key: "" });
      await refresh();
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  const setField = {
    setDefaultProvider: (value: string) => update("patchTranslation", { provider: value }),
    setDefaultTargetLang: (value: string) => update("patchTranslation", { targetLang: value }),
    setDefaultStyle: (value: string) => update("patchTranslation", { style: value }),
    setDefaultBatchSize: (value: number) => update("patchTranslation", { batchSize: value }),
    setDefaultMaxRetries: (value: number) => update("patchTranslation", { maxRetries: value }),
    setDefaultEnableSummary: (value: boolean) => update("patchTranslation", { enableSummary: value }),
    setOpenaiBaseUrl: (value: string) => update("patchLlm", { baseUrl: value }),
    setOpenaiModel: (value: string) => update("patchLlm", { model: value }),
    setOpenaiTemperature: (value: number) => update("patchLlm", { temperature: value }),
    setOpenaiTimeoutSeconds: (value: number) => update("patchLlm", { timeoutSeconds: value }),
    setOpenaiMaxRetries: (value: number) => update("patchLlm", { maxRetries: value }),
    setOpenaiApiType: (value: "openai" | "cerebras") => update("patchLlm", { apiType: value }),
    setOpenaiEnableThinking: (value: boolean) => update("patchLlm", { enableThinking: value }),
    setCerebrasReasoningEffort: (value: "low" | "medium" | "high") => update("patchLlm", { cerebrasReasoningEffort: value }),
    setCerebrasReasoningFormat: (value: "parsed" | "raw" | "hidden") => update("patchLlm", { cerebrasReasoningFormat: value }),
    setOpenaiApiKey: (value: string) => setSecrets((current) => ({ ...current, openaiApiKey: value })),
    setRagEnabled: (value: boolean) => update("patchRag", { enabled: value }),
    setRagTopK: (value: number) => update("patchRag", { topK: value }),
    setRagMinScore: (value: number) => update("patchRag", { minScore: value }),
    setRagEmbeddingProvider: (value: string) => update("patchEmbedding", { provider: value }),
    setRagEmbeddingModel: (value: string) => update("patchEmbedding", { model: value }),
    setRagEmbeddingDimensions: (value: number) => update("patchEmbedding", { dimensions: value }),
    setRagEmbeddingModelDir: (value: string) => update("patchEmbedding", { modelDir: value }),
    setRagEmbeddingDevice: (value: string) => update("patchEmbedding", { device: value }),
    setRagEmbeddingApiKey: (value: string) => setSecrets((current) => ({ ...current, embeddingApiKey: value })),
    setRagEmbeddingBaseUrl: (value: string) => update("patchEmbedding", { baseUrl: value }),
    setRagEmbeddingTimeoutSeconds: (value: number) => update("patchEmbedding", { timeoutSeconds: value }),
    setRagAutoDiscoverTerms: (value: boolean) => update("patchRag", { autoDiscoverTerms: value }),
    setRagAutoLearnTerms: (value: boolean) => update("patchRag", { autoLearnTerms: value }),
    setRagDictionaryEnabled: (value: boolean) => update("patchDictionary", { enabled: value }),
    setRagDictionaryTopK: (value: number) => update("patchDictionary", { topK: value }),
    setRagDictionaryMinQuality: (value: number) => update("patchDictionary", { minQuality: value }),
    setRagDictionaryAutoPromote: (value: boolean) => update("patchDictionary", { autoPromote: value }),
    setRagWikiEnabled: (value: boolean) => update("patchRag", { wikiEnabled: value }),
    setRagSearchEnabled: (value: boolean) => update("patchSearch", { enabled: value }),
    setRagSearchUrl: (value: string) => update("patchSearch", { url: value }),
    setRagSearchCategories: (value: string) => update("patchSearch", { categories: value }),
    setRagSearchEngines: (value: string) => update("patchSearch", { engines: value }),
    setRagSearchFallbackEngines: (value: string) => update("patchSearch", { fallbackEngines: value }),
    setRagSearchLanguage: (value: string) => update("patchSearch", { language: value }),
    setRagSearchSafesearch: (value: number) => update("patchSearch", { safesearch: value }),
    setRagSearchTimeRange: (value: string) => update("patchSearch", { timeRange: value }),
    setRagSearchPageno: (value: number) => update("patchSearch", { pageno: value }),
    setRagDomain: (value: string) => update("patchRag", { domain: value }),
    setRagAgentParallelism: (value: number) => update("patchAgent", { parallelism: value }),
    setRagAgentTimeoutSeconds: (value: number) => update("patchAgent", { timeoutSeconds: value }),
    setRagAgentSkillsEnabled: (value: boolean) => update("patchAgent", { skillsEnabled: value }),
    setRagAgentBuiltinSkillsEnabled: (value: boolean) => update("patchAgent", { builtinSkillsEnabled: value }),
    setRagAgentUserSkillsEnabled: (value: boolean) => update("patchAgent", { userSkillsEnabled: value }),
    setTestText: (value: string) => update("patchTest", { text: value }),
    setTestTargetLang: (value: string) => update("patchTest", { targetLang: value }),
    setTestStyle: (value: string) => update("patchTest", { style: value }),
    setEmbeddingDownloadModel: (value: string) => update("patchEmbeddingDownload", { model: value }),
    setEmbeddingDownloadName: (value: string) => update("patchEmbeddingDownload", { name: value }),
  };

  return {
    settings,
    embeddingModels,
    embeddingRuntime,
    agentSkills,
    error,
    busy,
    isDirty,
    activeTab,
    setActiveTab,
    refresh,
    saveSettings,
    discardChanges,
    rebuildKnowledgeEmbeddings,
    downloadEmbeddingModel,
    testEmbedding,
    testTranslation,
    clearOpenAiKey,
    clearEmbeddingKey,
    embeddingTestResult: results.embedding,
    embeddingRebuildResult: results.rebuild,
    testResult: results.translation,
    openaiApiKey: secrets.openaiApiKey,
    ragEmbeddingApiKey: secrets.embeddingApiKey,
    defaultProvider: form.translation.provider,
    defaultTargetLang: form.translation.targetLang,
    defaultStyle: form.translation.style,
    defaultBatchSize: form.translation.batchSize,
    defaultMaxRetries: form.translation.maxRetries,
    defaultEnableSummary: form.translation.enableSummary,
    openaiBaseUrl: form.llm.baseUrl,
    openaiModel: form.llm.model,
    openaiTemperature: form.llm.temperature,
    openaiTimeoutSeconds: form.llm.timeoutSeconds,
    openaiMaxRetries: form.llm.maxRetries,
    openaiApiType: form.llm.apiType,
    openaiEnableThinking: form.llm.enableThinking,
    cerebrasReasoningEffort: form.llm.cerebrasReasoningEffort,
    cerebrasReasoningFormat: form.llm.cerebrasReasoningFormat,
    ragEnabled: form.rag.enabled,
    ragTopK: form.rag.topK,
    ragMinScore: form.rag.minScore,
    ragEmbeddingProvider: form.embedding.provider,
    ragEmbeddingModel: form.embedding.model,
    ragEmbeddingDimensions: form.embedding.dimensions,
    ragEmbeddingModelDir: form.embedding.modelDir,
    ragEmbeddingDevice: form.embedding.device,
    ragEmbeddingBaseUrl: form.embedding.baseUrl,
    ragEmbeddingTimeoutSeconds: form.embedding.timeoutSeconds,
    ragAutoDiscoverTerms: form.rag.autoDiscoverTerms,
    ragAutoLearnTerms: form.rag.autoLearnTerms,
    ragDictionaryEnabled: form.rag.dictionary.enabled,
    ragDictionaryTopK: form.rag.dictionary.topK,
    ragDictionaryMinQuality: form.rag.dictionary.minQuality,
    ragDictionaryAutoPromote: form.rag.dictionary.autoPromote,
    ragWikiEnabled: form.rag.wikiEnabled,
    ragSearchEnabled: form.rag.search.enabled,
    ragSearchUrl: form.rag.search.url,
    ragSearchCategories: form.rag.search.categories,
    ragSearchEngines: form.rag.search.engines,
    ragSearchFallbackEngines: form.rag.search.fallbackEngines,
    ragSearchLanguage: form.rag.search.language,
    ragSearchSafesearch: form.rag.search.safesearch,
    ragSearchTimeRange: form.rag.search.timeRange,
    ragSearchPageno: form.rag.search.pageno,
    ragDomain: form.rag.domain,
    ragAgentParallelism: form.rag.agent.parallelism,
    ragAgentTimeoutSeconds: form.rag.agent.timeoutSeconds,
    ragAgentSkillsEnabled: form.rag.agent.skillsEnabled,
    ragAgentBuiltinSkillsEnabled: form.rag.agent.builtinSkillsEnabled,
    ragAgentUserSkillsEnabled: form.rag.agent.userSkillsEnabled,
    testText: form.test.text,
    testTargetLang: form.test.targetLang,
    testStyle: form.test.style,
    embeddingDownloadModel: form.embeddingDownload.model,
    embeddingDownloadName: form.embeddingDownload.name,
    ...setField,
  };
}

export type SettingsTranslateController = ReturnType<typeof useSettingsTranslateController>;
