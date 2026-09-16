import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, DataTable, Section } from "../../../components/ui";
import { csvItems, SEARXNG_CATEGORY_PRESETS, SEARXNG_ENGINE_PRESETS, toggleCsvItem } from "../form";
export function RagTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    agentSkills,
    busy,
    ragEnabled,
    setRagEnabled,
    ragTopK,
    setRagTopK,
    ragMinScore,
    setRagMinScore,
    ragEmbeddingProvider,
    setRagEmbeddingProvider,
    ragEmbeddingModel,
    setRagEmbeddingModel,
    ragEmbeddingDimensions,
    setRagEmbeddingDimensions,
    ragEmbeddingModelDir,
    setRagEmbeddingModelDir,
    ragEmbeddingDevice,
    setRagEmbeddingDevice,
    ragEmbeddingApiKey,
    setRagEmbeddingApiKey,
    ragEmbeddingBaseUrl,
    setRagEmbeddingBaseUrl,
    ragEmbeddingTimeoutSeconds,
    setRagEmbeddingTimeoutSeconds,
    ragAutoDiscoverTerms,
    setRagAutoDiscoverTerms,
    ragAutoLearnTerms,
    setRagAutoLearnTerms,
    ragDictionaryEnabled,
    setRagDictionaryEnabled,
    ragDictionaryTopK,
    setRagDictionaryTopK,
    ragDictionaryMinQuality,
    setRagDictionaryMinQuality,
    ragDictionaryAutoPromote,
    setRagDictionaryAutoPromote,
    ragWikiEnabled,
    setRagWikiEnabled,
    ragSearchEnabled,
    setRagSearchEnabled,
    ragSearchUrl,
    setRagSearchUrl,
    ragSearchCategories,
    setRagSearchCategories,
    ragSearchEngines,
    setRagSearchEngines,
    ragSearchFallbackEngines,
    setRagSearchFallbackEngines,
    ragSearchLanguage,
    setRagSearchLanguage,
    ragSearchSafesearch,
    setRagSearchSafesearch,
    ragSearchTimeRange,
    setRagSearchTimeRange,
    ragSearchPageno,
    setRagSearchPageno,
    ragDomain,
    setRagDomain,
    ragAgentParallelism,
    setRagAgentParallelism,
    ragAgentTimeoutSeconds,
    setRagAgentTimeoutSeconds,
    ragAgentSkillsEnabled,
    setRagAgentSkillsEnabled,
    ragAgentBuiltinSkillsEnabled,
    setRagAgentBuiltinSkillsEnabled,
    ragAgentUserSkillsEnabled,
    setRagAgentUserSkillsEnabled,
    embeddingTestResult,
    embeddingRebuildResult,
    rebuildKnowledgeEmbeddings,
    testEmbedding
  } = controller;
  return (
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
            <input type="checkbox" checked={ragDictionaryEnabled} onChange={(e) => setRagDictionaryEnabled(e.target.checked)} />
            启用导入词典查找
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={ragDictionaryAutoPromote} onChange={(e) => setRagDictionaryAutoPromote(e.target.checked)} />
            允许词典证据自动入库
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
            <div className="mb-1 text-xs text-slate-600">rag_dictionary_top_k</div>
            <input type="number" min={0} max={30} className="w-full rounded border px-3 py-2 text-sm" value={ragDictionaryTopK} onChange={(e) => setRagDictionaryTopK(parseInt(e.target.value || "0", 10))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">rag_dictionary_min_quality</div>
            <input type="number" step="0.01" min={0} max={1} className="w-full rounded border px-3 py-2 text-sm" value={ragDictionaryMinQuality} onChange={(e) => setRagDictionaryMinQuality(parseFloat(e.target.value || "0"))} />
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
          <div className="md:col-span-2 rounded-md border border-slate-200 p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-slate-900">Agent Skills</div>
                <div className="mt-1 text-xs text-slate-500">内置目录：src/videoroll/apps/subtitle_service/skills；用户目录：data/agent_skills。</div>
              </div>
              <div className="text-xs text-slate-500">{agentSkills.length} skills</div>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={ragAgentSkillsEnabled} onChange={(e) => setRagAgentSkillsEnabled(e.target.checked)} />
                启用 Agent Skills
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={ragAgentBuiltinSkillsEnabled} onChange={(e) => setRagAgentBuiltinSkillsEnabled(e.target.checked)} />
                读取内置 Skills
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={ragAgentUserSkillsEnabled} onChange={(e) => setRagAgentUserSkillsEnabled(e.target.checked)} />
                读取用户 Skills
              </label>
            </div>
            <div className="mt-3 max-h-64 overflow-auto rounded border border-slate-200">
              {agentSkills.length === 0 ? (
                <div className="p-3 text-sm text-slate-500">未发现 skill。每个 skill 使用独立目录，并放入 skill.json 或 SKILL.md。</div>
              ) : (
                <DataTable>
                  <thead>
                    <tr>
                      <th className="py-2 pr-3 text-left">Name</th>
                      <th className="py-2 pr-3 text-left">Source</th>
                      <th className="py-2 pr-3 text-left">Tools</th>
                      <th className="py-2 pr-3 text-left">Triggers</th>
                      <th className="py-2 pr-3 text-left">Resources</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agentSkills.map((skill) => (
                      <tr key={`${skill.source}:${skill.name}`}>
                        <td className="py-2 pr-3">
                          <div className="font-mono text-xs text-slate-900">{skill.name}</div>
                          <div className="mt-1 max-w-md truncate text-xs text-slate-500">{skill.description || "-"}</div>
                        </td>
                        <td className="py-2 pr-3 text-xs">{skill.source}</td>
                        <td className="py-2 pr-3 text-xs">{skill.allowed_tools.length ? skill.allowed_tools.join(", ") : "all"}</td>
                        <td className="py-2 pr-3 text-xs">{[...skill.domain, ...skill.triggers].slice(0, 6).join(", ") || "-"}</td>
                        <td className="py-2 pr-3 text-xs">{skill.resource_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
              )}
            </div>
          </div>
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
          <div className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">SearXNG categories</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragSearchCategories} onChange={(e) => setRagSearchCategories(e.target.value)} />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {SEARXNG_CATEGORY_PRESETS.map((item) => {
                const selected = csvItems(ragSearchCategories).some((x) => x.toLowerCase() === item.toLowerCase());
                return (
                  <button
                    key={item}
                    type="button"
                    className={`rounded border px-2 py-1 text-xs ${selected ? "border-sky-400 bg-sky-50 text-sky-800" : "border-slate-200 text-slate-600 hover:bg-slate-50"}`}
                    onClick={() => setRagSearchCategories(toggleCsvItem(ragSearchCategories, item))}
                  >
                    {item}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">SearXNG engines</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="留空则使用实例默认引擎" value={ragSearchEngines} onChange={(e) => setRagSearchEngines(e.target.value)} />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {SEARXNG_ENGINE_PRESETS.map((item) => {
                const selected = csvItems(ragSearchEngines).some((x) => x.toLowerCase() === item.toLowerCase());
                return (
                  <button
                    key={item}
                    type="button"
                    className={`rounded border px-2 py-1 text-xs ${selected ? "border-sky-400 bg-sky-50 text-sky-800" : "border-slate-200 text-slate-600 hover:bg-slate-50"}`}
                    onClick={() => setRagSearchEngines(toggleCsvItem(ragSearchEngines, item))}
                  >
                    {item}
                  </button>
                );
              })}
            </div>
          </div>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">SearXNG fallback_engines</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragSearchFallbackEngines} onChange={(e) => setRagSearchFallbackEngines(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">SearXNG language</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={ragSearchLanguage} onChange={(e) => setRagSearchLanguage(e.target.value)}>
              <option value="all">all</option>
              <option value="zh-CN">zh-CN</option>
              <option value="zh-TW">zh-TW</option>
              <option value="en-US">en-US</option>
              <option value="ja-JP">ja-JP</option>
              <option value="ko-KR">ko-KR</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">SearXNG safesearch</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={ragSearchSafesearch} onChange={(e) => setRagSearchSafesearch(parseInt(e.target.value || "0", 10))}>
              <option value={0}>0</option>
              <option value={1}>1</option>
              <option value={2}>2</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">SearXNG time_range</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={ragSearchTimeRange} onChange={(e) => setRagSearchTimeRange(e.target.value)}>
              <option value="">不限</option>
              <option value="day">day</option>
              <option value="month">month</option>
              <option value="year">year</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">SearXNG pageno</div>
            <input type="number" min={1} max={100} className="w-full rounded border px-3 py-2 text-sm" value={ragSearchPageno} onChange={(e) => setRagSearchPageno(parseInt(e.target.value || "1", 10))} />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button disabled={busy} onClick={testEmbedding}>测试 Embedding</Button>
          <Button tone="primary" disabled={busy} onClick={rebuildKnowledgeEmbeddings}>{busy ? "处理中..." : "重建知识库向量"}</Button>
          {embeddingTestResult ? <div className="text-sm text-slate-700">{embeddingTestResult}</div> : null}
          {embeddingRebuildResult ? <div className="text-sm text-slate-700">{embeddingRebuildResult}</div> : null}
        </div>
      </Section>
  );
}
