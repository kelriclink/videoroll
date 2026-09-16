import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, DataTable, EmptyState, Section } from "../../../components/ui";
import { safeEmbeddingModelName } from "../form";
export function EmbeddingTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    embeddingModels,
    busy,
    setRagEmbeddingProvider,
    setRagEmbeddingModel,
    setRagEmbeddingDimensions,
    embeddingDownloadModel,
    setEmbeddingDownloadModel,
    embeddingDownloadName,
    setEmbeddingDownloadName,
    downloadEmbeddingModel
  } = controller;
  return (
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
            填入表单
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
  );
}
