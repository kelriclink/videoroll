import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useConfirm } from "../components/feedbackContext";
import { Button, DataTable, EmptyState, PageHeader, Section } from "../components/ui";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type TranslateSettings = {
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
  rag_wiki_enabled: boolean;
  rag_search_enabled: boolean;
  rag_search_url: string;
  rag_domain: string;
  rag_agent_parallelism: number;
  rag_agent_timeout_seconds: number;
};

type EmbeddingModelInfo = {
  name: string;
  path: string;
  size_bytes?: number | null;
};

function safeEmbeddingModelName(raw: string) {
  const value = raw.trim().replace(/[\\/]/g, "--").replace(/[^A-Za-z0-9._-]/g, "-");
  return value.slice(0, 96) || "embedding-model";
}

export default function SettingsTranslatePage() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<TranslateSettings | null>(null);
  const [embeddingModels, setEmbeddingModels] = useState<EmbeddingModelInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [defaultProvider, setDefaultProvider] = useState("openai");
  const [defaultTargetLang, setDefaultTargetLang] = useState("zh");
  const [defaultStyle, setDefaultStyle] = useState("口语自然");
  const [defaultBatchSize, setDefaultBatchSize] = useState(50);
  const [defaultMaxRetries, setDefaultMaxRetries] = useState(2);
  const [defaultEnableSummary, setDefaultEnableSummary] = useState(true);

  const [openaiBaseUrl, setOpenaiBaseUrl] = useState("https://api.openai.com/v1");
  const [openaiModel, setOpenaiModel] = useState("gpt-4o-mini");
  const [openaiTemperature, setOpenaiTemperature] = useState(0.2);
  const [openaiTimeoutSeconds, setOpenaiTimeoutSeconds] = useState(60);
  const [openaiApiKey, setOpenaiApiKey] = useState("");

  const [ragEnabled, setRagEnabled] = useState(false);
  const [ragTopK, setRagTopK] = useState(8);
  const [ragMinScore, setRagMinScore] = useState(0.68);
  const [ragEmbeddingProvider, setRagEmbeddingProvider] = useState("openai");
  const [ragEmbeddingModel, setRagEmbeddingModel] = useState("text-embedding-3-small");
  const [ragEmbeddingDimensions, setRagEmbeddingDimensions] = useState(1536);
  const [ragEmbeddingModelDir, setRagEmbeddingModelDir] = useState("/models/embeddings");
  const [ragEmbeddingDevice, setRagEmbeddingDevice] = useState("cpu");
  const [ragEmbeddingApiKey, setRagEmbeddingApiKey] = useState("");
  const [ragEmbeddingBaseUrl, setRagEmbeddingBaseUrl] = useState("https://api.openai.com/v1");
  const [ragEmbeddingTimeoutSeconds, setRagEmbeddingTimeoutSeconds] = useState(60);
  const [ragAutoDiscoverTerms, setRagAutoDiscoverTerms] = useState(false);
  const [ragAutoLearnTerms, setRagAutoLearnTerms] = useState(false);
  const [ragWikiEnabled, setRagWikiEnabled] = useState(false);
  const [ragSearchEnabled, setRagSearchEnabled] = useState(false);
  const [ragSearchUrl, setRagSearchUrl] = useState("");
  const [ragDomain, setRagDomain] = useState("");
  const [ragAgentParallelism, setRagAgentParallelism] = useState(1);
  const [ragAgentTimeoutSeconds, setRagAgentTimeoutSeconds] = useState(120);

  const [testText, setTestText] = useState("Hello world. This is a translation test.");
  const [testTargetLang, setTestTargetLang] = useState("zh");
  const [testStyle, setTestStyle] = useState("口语自然");
  const [testResult, setTestResult] = useState<string | null>(null);

  const [embeddingDownloadModel, setEmbeddingDownloadModel] = useState("BAAI/bge-small-zh-v1.5");
  const [embeddingDownloadName, setEmbeddingDownloadName] = useState("");
  const [embeddingTestResult, setEmbeddingTestResult] = useState<string | null>(null);
  const [embeddingRebuildResult, setEmbeddingRebuildResult] = useState<string | null>(null);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<TranslateSettings>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`);
      const localModels = await fetchJson<EmbeddingModelInfo[]>(`${SUBTITLE_SERVICE_URL}/subtitle/embedding/models/list`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_dir: s.rag_embedding_model_dir }),
      }).catch(() => []);
      setSettings(s);
      setDefaultProvider(s.default_provider);
      setDefaultTargetLang(s.default_target_lang);
      setDefaultStyle(s.default_style);
      setDefaultBatchSize(s.default_batch_size);
      setDefaultMaxRetries(s.default_max_retries ?? 0);
      setDefaultEnableSummary(s.default_enable_summary);
      setOpenaiBaseUrl(s.openai_base_url);
      setOpenaiModel(s.openai_model);
      setOpenaiTemperature(s.openai_temperature);
      setOpenaiTimeoutSeconds(s.openai_timeout_seconds);
      setRagEnabled(s.rag_enabled);
      setRagTopK(s.rag_top_k);
      setRagMinScore(s.rag_min_score);
      setRagEmbeddingProvider(s.rag_embedding_provider);
      setRagEmbeddingModel(s.rag_embedding_model);
      setRagEmbeddingDimensions(s.rag_embedding_dimensions);
      setRagEmbeddingModelDir(s.rag_embedding_model_dir);
      setRagEmbeddingDevice(s.rag_embedding_device);
      setRagEmbeddingBaseUrl(s.rag_embedding_base_url);
      setRagEmbeddingTimeoutSeconds(s.rag_embedding_timeout_seconds);
      setRagAutoDiscoverTerms(s.rag_auto_discover_terms);
      setRagAutoLearnTerms(s.rag_auto_learn_terms);
      setRagWikiEnabled(s.rag_wiki_enabled ?? false);
      setRagSearchEnabled(s.rag_search_enabled);
      setRagSearchUrl(s.rag_search_url);
      setRagDomain(s.rag_domain);
      setRagAgentParallelism(s.rag_agent_parallelism ?? 1);
      setRagAgentTimeoutSeconds(s.rag_agent_timeout_seconds ?? 120);
      setTestTargetLang(s.default_target_lang || "zh");
      setTestStyle(s.default_style || "口语自然");
      setEmbeddingModels(localModels);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function persistSettings() {
    const payload: Partial<TranslateSettings> & { openai_api_key?: string; rag_embedding_api_key?: string } = {
      default_provider: defaultProvider,
      default_target_lang: defaultTargetLang,
      default_style: defaultStyle,
      default_batch_size: defaultBatchSize,
      default_max_retries: defaultMaxRetries,
      default_enable_summary: defaultEnableSummary,
      openai_base_url: openaiBaseUrl,
      openai_model: openaiModel,
      openai_temperature: openaiTemperature,
      openai_timeout_seconds: openaiTimeoutSeconds,
      rag_enabled: ragEnabled,
      rag_top_k: ragTopK,
      rag_min_score: ragMinScore,
      rag_embedding_provider: ragEmbeddingProvider,
      rag_embedding_model: ragEmbeddingModel,
      rag_embedding_dimensions: ragEmbeddingDimensions,
      rag_embedding_model_dir: ragEmbeddingModelDir,
      rag_embedding_device: ragEmbeddingDevice,
      rag_embedding_base_url: ragEmbeddingBaseUrl,
      rag_embedding_timeout_seconds: ragEmbeddingTimeoutSeconds,
      rag_auto_discover_terms: ragAutoDiscoverTerms,
      rag_auto_learn_terms: ragAutoLearnTerms,
      rag_wiki_enabled: ragWikiEnabled,
      rag_search_enabled: ragSearchEnabled,
      rag_search_url: ragSearchUrl,
      rag_domain: ragDomain,
      rag_agent_parallelism: ragAgentParallelism,
      rag_agent_timeout_seconds: ragAgentTimeoutSeconds,
    };
    if (openaiApiKey.trim()) payload.openai_api_key = openaiApiKey.trim();
    if (ragEmbeddingApiKey.trim()) payload.rag_embedding_api_key = ragEmbeddingApiKey.trim();
    await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async function saveSettings() {
    setBusy(true);
    setError(null);
    try {
      await persistSettings();
      setOpenaiApiKey("");
      setRagEmbeddingApiKey("");
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
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
    setEmbeddingRebuildResult(null);
    try {
      await persistSettings();
      setOpenaiApiKey("");
      setRagEmbeddingApiKey("");
      const resp = await fetchJson<{
        total: number;
        updated: number;
        failed: number;
        skipped: number;
        embedding_model: string;
        dimensions: number;
      }>(`${SUBTITLE_SERVICE_URL}/subtitle/knowledge/rebuild-embeddings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 10000 }),
      });
      setEmbeddingRebuildResult(
        `${resp.embedding_model} / ${resp.dimensions} dims：共 ${resp.total} 条，更新 ${resp.updated}，跳过 ${resp.skipped}，失败 ${resp.failed}`,
      );
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function downloadEmbeddingModel() {
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/embedding/models/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: embeddingDownloadModel,
          name: embeddingDownloadName.trim() || null,
          model_dir: ragEmbeddingModelDir,
          force: false,
        }),
      });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function testEmbedding() {
    setBusy(true);
    setError(null);
    setEmbeddingTestResult(null);
    try {
      const resp = await fetchJson<{ provider: string; model: string; dimensions: number; expected_dimensions: number; ok: boolean }>(
        `${SUBTITLE_SERVICE_URL}/subtitle/embedding/test`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: "Rush B with an AWP",
            provider: ragEmbeddingProvider,
            model: ragEmbeddingModel,
            model_dir: ragEmbeddingModelDir,
            dimensions: ragEmbeddingDimensions,
            device: ragEmbeddingDevice,
            api_key: ragEmbeddingApiKey.trim() || undefined,
            base_url: ragEmbeddingBaseUrl,
            timeout_seconds: ragEmbeddingTimeoutSeconds,
          }),
        },
      );
      setEmbeddingTestResult(`${resp.provider}:${resp.model} -> ${resp.dimensions} dims${resp.ok ? "" : `，期望 ${resp.expected_dimensions}`}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Settings · Translate"
        description="配置 OpenAI 翻译、RAG Gate、pgvector 和 embedding 模型。"
        actions={
          <>
            <Link to="/knowledge" className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50">
              知识库
            </Link>
            <Button onClick={() => refresh()}>刷新</Button>
          </>
        }
      />
      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}

      <Section>
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-slate-900">当前配置</div>
          <div className="text-xs text-slate-500">
            OpenAI API Key：{settings?.openai_api_key_set ? <span className="text-emerald-700">已设置</span> : <span className="text-rose-700">未设置</span>}
          </div>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中...</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            {[
              ["provider", settings.default_provider],
              ["target", settings.default_target_lang],
              ["batch", settings.default_batch_size],
              ["summary", settings.default_enable_summary ? "true" : "false"],
              ["rag", settings.rag_enabled ? "enabled" : "disabled"],
              ["wiki", settings.rag_wiki_enabled ? "enabled" : "disabled"],
              ["agents", `${settings.rag_agent_parallelism} / ${settings.rag_agent_timeout_seconds}s`],
              ["embedding", `${settings.rag_embedding_provider}:${settings.rag_embedding_model}`],
              ["embedding key", settings.rag_embedding_api_key_set ? "set" : "unset"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-md border border-slate-200 p-3">
                <div className="text-xs text-slate-500">{label}</div>
                <div className="mt-1 break-all font-mono text-sm text-slate-900">{value}</div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section>
        <div className="text-sm font-semibold text-slate-900">翻译与模型</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_provider</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultProvider} onChange={(e) => setDefaultProvider(e.target.value)}>
              <option value="openai">openai</option>
              <option value="mock">mock</option>
              <option value="noop">noop</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_target_lang</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={defaultTargetLang} onChange={(e) => setDefaultTargetLang(e.target.value)} />
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">default_style</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultStyle} onChange={(e) => setDefaultStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_batch_size</div>
            <input type="number" min={1} className="w-full rounded border px-3 py-2 text-sm" value={defaultBatchSize} onChange={(e) => setDefaultBatchSize(parseInt(e.target.value || "1", 10))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_max_retries</div>
            <input type="number" min={0} max={10} className="w-full rounded border px-3 py-2 text-sm" value={defaultMaxRetries} onChange={(e) => setDefaultMaxRetries(parseInt(e.target.value || "0", 10))} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={defaultEnableSummary} onChange={(e) => setDefaultEnableSummary(e.target.checked)} />
            default_enable_summary
          </label>
          <div className="md:col-span-2 pt-2 text-xs font-semibold text-slate-700">OpenAI（标准接口）</div>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_api_key（仅保存，不回显）</div>
            <input type="password" className="w-full rounded border px-3 py-2 text-sm" placeholder={settings?.openai_api_key_set ? "已设置（留空则不修改）" : "sk-..."} value={openaiApiKey} onChange={(e) => setOpenaiApiKey(e.target.value)} />
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_base_url</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiBaseUrl} onChange={(e) => setOpenaiBaseUrl(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_model</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiModel} onChange={(e) => setOpenaiModel(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_temperature</div>
            <input type="number" step="0.1" min={0} max={2} className="w-full rounded border px-3 py-2 text-sm" value={openaiTemperature} onChange={(e) => setOpenaiTemperature(parseFloat(e.target.value || "0"))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_timeout_seconds</div>
            <input type="number" min={1} className="w-full rounded border px-3 py-2 text-sm" value={openaiTimeoutSeconds} onChange={(e) => setOpenaiTimeoutSeconds(parseFloat(e.target.value || "1"))} />
          </label>
        </div>
      </Section>

      <Section>
        <div className="text-sm font-semibold text-slate-900">RAG / pgvector</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragEnabled} onChange={(e) => setRagEnabled(e.target.checked)} />
            启用 RAG 翻译增强
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragAutoDiscoverTerms} onChange={(e) => setRagAutoDiscoverTerms(e.target.checked)} />
            LLM 自动发现术语
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragAutoLearnTerms} onChange={(e) => setRagAutoLearnTerms(e.target.checked)} />
            允许自动学习术语
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragWikiEnabled} onChange={(e) => setRagWikiEnabled(e.target.checked)} />
            允许调用 Wikipedia Tool
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragSearchEnabled} onChange={(e) => setRagSearchEnabled(e.target.checked)} />
            允许调用搜索服务
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">rag_top_k</div>
            <input type="number" min={0} max={30} className="w-full rounded border px-3 py-2 text-sm" value={ragTopK} onChange={(e) => setRagTopK(parseInt(e.target.value || "0", 10))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">rag_min_score</div>
            <input type="number" step="0.01" min={0} max={1} className="w-full rounded border px-3 py-2 text-sm" value={ragMinScore} onChange={(e) => setRagMinScore(parseFloat(e.target.value || "0"))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">rag_agent_parallelism</div>
            <input type="number" min={1} max={8} className="w-full rounded border px-3 py-2 text-sm" value={ragAgentParallelism} onChange={(e) => setRagAgentParallelism(parseInt(e.target.value || "1", 10))} />
            <div className="mt-1 text-xs text-slate-500">同一个字幕 batch 内最多并行研究几个术语。</div>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">rag_agent_timeout_seconds</div>
            <input type="number" min={10} max={900} className="w-full rounded border px-3 py-2 text-sm" value={ragAgentTimeoutSeconds} onChange={(e) => setRagAgentTimeoutSeconds(parseFloat(e.target.value || "120"))} />
            <div className="mt-1 text-xs text-slate-500">并行 agent 等待预算，超时后继续翻译。</div>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">embedding_model</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingModel} onChange={(e) => setRagEmbeddingModel(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">embedding_provider</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={ragEmbeddingProvider}
              onChange={(e) => {
                const provider = e.target.value;
                setRagEmbeddingProvider(provider);
                if (provider === "local" && ragEmbeddingModel === "text-embedding-3-small") {
                  setRagEmbeddingModel("BAAI/bge-small-zh-v1.5");
                  setRagEmbeddingDimensions(512);
                }
                if (provider === "openai" && ragEmbeddingModel === "BAAI/bge-small-zh-v1.5") {
                  setRagEmbeddingModel("text-embedding-3-small");
                  setRagEmbeddingDimensions(1536);
                }
              }}
            >
              <option value="openai">openai</option>
              <option value="local">local</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">embedding_dimensions</div>
            <input type="number" min={1} max={4096} className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingDimensions} onChange={(e) => setRagEmbeddingDimensions(parseInt(e.target.value || "1536", 10))} />
          </label>
          {ragEmbeddingProvider === "openai" ? (
            <>
              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">embedding_api_key（独立于翻译 API Key，不回显）</div>
                <input
                  type="password"
                  className="w-full rounded border px-3 py-2 text-sm"
                  placeholder={settings?.rag_embedding_api_key_set ? "已设置（留空则不修改）" : "embedding API key"}
                  value={ragEmbeddingApiKey}
                  onChange={(e) => setRagEmbeddingApiKey(e.target.value)}
                />
              </label>
              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">embedding_base_url（独立于翻译 base URL）</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingBaseUrl} onChange={(e) => setRagEmbeddingBaseUrl(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">embedding_timeout_seconds</div>
                <input type="number" min={1} className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingTimeoutSeconds} onChange={(e) => setRagEmbeddingTimeoutSeconds(parseFloat(e.target.value || "1"))} />
              </label>
            </>
          ) : null}
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">embedding_device</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingDevice} onChange={(e) => setRagEmbeddingDevice(e.target.value)}>
              <option value="cpu">CPU（PyTorch）</option>
              <option value="openvino:CPU">CPU（OpenVINO）</option>
              <option value="openvino:GPU">Intel GPU（OpenVINO）</option>
            </select>
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">embedding_model_dir</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingModelDir} onChange={(e) => setRagEmbeddingModelDir(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">domain</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="例如 Minecraft / CS2 / Anime" value={ragDomain} onChange={(e) => setRagDomain(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">SearXNG Base URL</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="https://search.linvk.com" value={ragSearchUrl} onChange={(e) => setRagSearchUrl(e.target.value)} />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button disabled={busy} onClick={testEmbedding}>测试 Embedding</Button>
          <Button tone="primary" disabled={busy} onClick={rebuildKnowledgeEmbeddings}>{busy ? "处理中..." : "重建知识库向量"}</Button>
          {embeddingTestResult ? <div className="text-sm text-slate-700">{embeddingTestResult}</div> : null}
          {embeddingRebuildResult ? <div className="text-sm text-slate-700">{embeddingRebuildResult}</div> : null}
        </div>
      </Section>

      <Section>
        <div className="text-sm font-semibold text-slate-900">本地 Embedding 模型</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">HuggingFace repo / alias</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={embeddingDownloadModel} onChange={(e) => setEmbeddingDownloadModel(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">保存名称（可空）</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="默认用 repo 名称" value={embeddingDownloadName} onChange={(e) => setEmbeddingDownloadName(e.target.value)} />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button tone="primary" disabled={busy} onClick={downloadEmbeddingModel}>{busy ? "下载中..." : "下载模型"}</Button>
          <Button
            disabled={busy}
            onClick={() => {
              setRagEmbeddingProvider("local");
              setRagEmbeddingModel(safeEmbeddingModelName(embeddingDownloadName.trim() || embeddingDownloadModel));
              if (embeddingDownloadModel.includes("bge-small")) setRagEmbeddingDimensions(512);
            }}
          >
            使用该模型
          </Button>
        </div>
        {embeddingModels.length === 0 ? (
          <EmptyState>暂无本地 embedding 模型</EmptyState>
        ) : (
          <DataTable>
            <thead>
              <tr>
                <th className="py-2 pr-3 text-left">Name</th>
                <th className="py-2 pr-3 text-left">Path</th>
                <th className="py-2 pr-3 text-left">Size</th>
                <th className="py-2 pr-3 text-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {embeddingModels.map((model) => (
                <tr key={model.name}>
                  <td className="py-2 pr-3 font-mono text-xs">{model.name}</td>
                  <td className="py-2 pr-3 font-mono text-xs">{model.path}</td>
                  <td className="py-2 pr-3">{model.size_bytes ? `${Math.round(model.size_bytes / 1024 / 1024)} MB` : "-"}</td>
                  <td className="py-2 pr-3">
                    <Button
                      size="xs"
                      onClick={() => {
                        setRagEmbeddingProvider("local");
                        setRagEmbeddingModel(model.name);
                        if (model.name.includes("bge-small")) setRagEmbeddingDimensions(512);
                      }}
                    >
                      使用
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Section>

      <div className="flex flex-wrap items-center gap-2">
        <Button tone="primary" disabled={busy} onClick={saveSettings}>{busy ? "保存中..." : "保存配置"}</Button>
        <Button
          tone="danger"
          disabled={busy || !settings?.openai_api_key_set}
          onClick={async () => {
            const ok = await confirm({ title: "清除 OpenAI API Key", message: "确定清除 OpenAI API Key 吗？", confirmLabel: "清除", tone: "danger" });
            if (!ok) return;
            setBusy(true);
            setError(null);
            try {
              await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ openai_api_key: "" }),
              });
              await refresh();
            } catch (e: unknown) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          清除 Key
        </Button>
        <Button
          tone="danger"
          disabled={busy || !settings?.rag_embedding_api_key_set}
          onClick={async () => {
            const ok = await confirm({ title: "清除 Embedding API Key", message: "确定清除 OpenAI 兼容 embedding 的 API Key 吗？", confirmLabel: "清除", tone: "danger" });
            if (!ok) return;
            setBusy(true);
            setError(null);
            try {
              await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rag_embedding_api_key: "" }),
              });
              await refresh();
            } catch (e: unknown) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          清除 Embedding Key
        </Button>
      </div>

      <Section>
        <div className="text-sm font-semibold text-slate-900">测试翻译</div>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">target_lang</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={testTargetLang} onChange={(e) => setTestTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">style</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={testStyle} onChange={(e) => setTestStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">text</div>
            <textarea className="h-28 w-full rounded border px-3 py-2 text-sm" value={testText} onChange={(e) => setTestText(e.target.value)} />
          </label>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <Button
            tone="primary"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              setTestResult(null);
              try {
                const resp = await fetchJson<{ translated_text: string }>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/test`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ text: testText, target_lang: testTargetLang, style: testStyle }),
                });
                setTestResult(resp.translated_text);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "测试中..." : "开始测试"}
          </Button>
          {testResult ? <div className="text-sm text-slate-700">OK</div> : null}
        </div>
        {testResult ? (
          <div className="mt-3 rounded border bg-slate-50 p-3">
            <div className="text-xs text-slate-500">translated_text</div>
            <div className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{testResult}</div>
          </div>
        ) : null}
      </Section>
    </div>
  );
}
