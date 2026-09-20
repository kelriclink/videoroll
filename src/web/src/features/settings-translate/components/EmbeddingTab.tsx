import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, DataTable, EmptyState, Section } from "../../../components/ui";
import { safeEmbeddingModelName } from "../form";
export function EmbeddingTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    embeddingModels,
    embeddingRuntime,
    busy,
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
    embeddingTestResult,
    embeddingRebuildResult,
    testEmbedding,
    rebuildKnowledgeEmbeddings,
    embeddingDownloadModel,
    setEmbeddingDownloadModel,
    embeddingDownloadName,
    setEmbeddingDownloadName,
    downloadEmbeddingModel
  } = controller;
  return (
    <div className="space-y-4">
      <Section>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-900">向量运行状态</div>
            <div className="mt-1 text-xs text-slate-500">直接读取生产数据库和 pgvector 元数据，不会生成新向量。</div>
          </div>
          <span
            className={[
              "rounded-full px-2.5 py-1 text-xs font-medium",
              embeddingRuntime?.search_mode === "hnsw"
                ? "bg-emerald-50 text-emerald-700"
                : embeddingRuntime?.search_mode === "exact_scan"
                  ? "bg-amber-50 text-amber-700"
                  : "bg-slate-100 text-slate-500",
            ].join(" ")}
          >
            {embeddingRuntime?.search_mode === "hnsw"
              ? "HNSW"
              : embeddingRuntime?.search_mode === "exact_scan"
                ? "Exact scan"
                : "Unavailable"}
          </span>
        </div>
        {!embeddingRuntime ? (
          <div className="mt-3 text-sm text-slate-500">运行状态暂不可用。</div>
        ) : (
          <>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-lg border border-slate-200 p-3">
                <div className="text-xs text-slate-500">当前模型</div>
                <div className="mt-1 truncate text-sm font-medium text-slate-900" title={embeddingRuntime.model}>{embeddingRuntime.model || "-"}</div>
                <div className="mt-1 text-xs text-slate-500">{embeddingRuntime.configured_dimensions}d · {embeddingRuntime.device}</div>
              </div>
              <div className="rounded-lg border border-slate-200 p-3">
                <div className="text-xs text-slate-500">当前模型向量</div>
                <div className="mt-1 text-lg font-semibold text-slate-900">{embeddingRuntime.active_embeddings}</div>
                <div className="mt-1 text-xs text-slate-500">总向量 {embeddingRuntime.total_embeddings}</div>
              </div>
              <div className="rounded-lg border border-slate-200 p-3">
                <div className="text-xs text-slate-500">pgvector</div>
                <div className="mt-1 text-sm font-medium text-slate-900">{embeddingRuntime.pgvector_version || "未检测"}</div>
                <div className="mt-1 font-mono text-xs text-slate-500">{embeddingRuntime.column_type || "-"}</div>
              </div>
              <div className="rounded-lg border border-slate-200 p-3">
                <div className="text-xs text-slate-500">HNSW</div>
                <div className="mt-1 text-sm font-medium text-slate-900">
                  {embeddingRuntime.hnsw_usable_for_current_query ? "当前查询可用" : embeddingRuntime.hnsw_index_present ? "索引存在但不可用于当前查询" : "未启用"}
                </div>
                <div className="mt-1 text-xs text-slate-500">{embeddingRuntime.search_mode === "exact_scan" ? "当前使用精确余弦扫描" : "ANN 检索"}</div>
              </div>
            </div>
            {embeddingRuntime.buckets.length ? (
              <div className="mt-3 overflow-x-auto rounded-lg border border-slate-200">
                <table className="min-w-full text-sm">
                  <thead className="bg-slate-50 text-xs text-slate-500">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Embedding model</th>
                      <th className="px-3 py-2 text-right font-medium">Dimensions</th>
                      <th className="px-3 py-2 text-right font-medium">Vectors</th>
                    </tr>
                  </thead>
                  <tbody>
                    {embeddingRuntime.buckets.map((bucket) => (
                      <tr key={`${bucket.embedding_model}:${bucket.dimensions}`} className="border-t border-slate-100">
                        <td className="max-w-xl truncate px-3 py-2 font-mono text-xs" title={bucket.embedding_model}>{bucket.embedding_model || "(legacy)"}</td>
                        <td className="px-3 py-2 text-right">{bucket.dimensions}</td>
                        <td className="px-3 py-2 text-right">{bucket.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {embeddingRuntime.detail ? (
              <div className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">{embeddingRuntime.detail}</div>
            ) : null}
          </>
        )}
      </Section>

      <Section>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-900">Embedding 配置</div>
            <div className="mt-1 text-xs text-slate-500">统一管理 provider、模型、维度、设备和独立 API。RAG 页不再重复这些字段。</div>
          </div>
          <div className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600">
            {ragEmbeddingDimensions}d · {ragEmbeddingDevice}
          </div>
        </div>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">Provider</div>
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
              <option value="openai">OpenAI compatible</option>
              <option value="local">Local</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">向量维度</div>
            <input type="number" min={1} max={4096} className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingDimensions} onChange={(e) => setRagEmbeddingDimensions(parseInt(e.target.value || "1536", 10))} />
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">模型</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingModel} onChange={(e) => setRagEmbeddingModel(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">运行设备</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingDevice} onChange={(e) => setRagEmbeddingDevice(e.target.value)}>
              <option value="cpu">CPU（PyTorch）</option>
              <option value="openvino:CPU">CPU（OpenVINO）</option>
              <option value="openvino:GPU">Intel GPU（OpenVINO）</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">请求超时（秒）</div>
            <input type="number" min={1} className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingTimeoutSeconds} onChange={(e) => setRagEmbeddingTimeoutSeconds(parseFloat(e.target.value || "1"))} />
          </label>
          {ragEmbeddingProvider === "openai" ? (
            <>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs text-slate-600">Embedding API Key（独立于翻译 Key，不回显）</div>
                <input
                  type="password"
                  className="w-full rounded border px-3 py-2 text-sm"
                  placeholder={settings?.rag_embedding_api_key_set ? "已设置（留空则不修改）" : "embedding API key"}
                  value={ragEmbeddingApiKey}
                  onChange={(e) => setRagEmbeddingApiKey(e.target.value)}
                />
              </label>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs text-slate-600">Embedding Base URL</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingBaseUrl} onChange={(e) => setRagEmbeddingBaseUrl(e.target.value)} />
              </label>
            </>
          ) : null}
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">本地模型目录</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={ragEmbeddingModelDir} onChange={(e) => setRagEmbeddingModelDir(e.target.value)} />
          </label>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
          <Button disabled={busy} onClick={testEmbedding}>测试 Embedding</Button>
          <Button tone="primary" disabled={busy} onClick={rebuildKnowledgeEmbeddings}>{busy ? "处理中..." : "重建知识库向量"}</Button>
          {embeddingTestResult ? <div className="text-sm text-slate-700">{embeddingTestResult}</div> : null}
          {embeddingRebuildResult ? <div className="basis-full text-sm text-slate-700">{embeddingRebuildResult}</div> : null}
        </div>
      </Section>

      <Section>
        <div className="text-sm font-semibold text-slate-900">本地 Embedding 模型</div>
        <div className="mt-1 text-xs text-slate-500">下载或选择本地模型后，可直接填入上面的运行配置。</div>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
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
            填入配置
          </Button>
        </div>
        <div className="mt-4">
          {embeddingModels.length === 0 ? (
            <EmptyState>暂无本地 embedding 模型</EmptyState>
          ) : (
            <DataTable>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Path</th>
                  <th>Size</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {embeddingModels.map((model) => (
                  <tr key={model.name}>
                    <td className="font-mono text-xs">{model.name}</td>
                    <td className="max-w-80 truncate font-mono text-xs" title={model.path}>{model.path}</td>
                    <td>{model.size_bytes ? `${Math.round(model.size_bytes / 1024 / 1024)} MB` : "-"}</td>
                    <td>
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
        </div>
      </Section>
    </div>
  );
}
