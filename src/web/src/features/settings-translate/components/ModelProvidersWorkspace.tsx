import { useCallback, useEffect, useState } from "react";
import {
  translateApi,
  type ModelProvider,
  type ModelProviderDraft,
  type ProviderModel,
} from "../../../api/translate";
import { useConfirm } from "../../../components/feedbackContext";
import { Button, InlineNotice, Section } from "../../../components/ui";
import { IndexTranslateOptions } from "../model-providers/IndexTranslateOptions";
import { ModelProviderConnectionForm } from "../model-providers/ModelProviderConnectionForm";
import { ModelProviderModelsEditor } from "../model-providers/ModelProviderModelsEditor";
import { ModelProviderSidebar } from "../model-providers/ModelProviderSidebar";
import {
  emptyDraft,
  newModel,
  toDraft,
  validateProviderDraft,
  type RemoteModelDetails,
} from "../model-providers/modelProviderForm";

export function ModelProvidersWorkspace() {
  const confirm = useConfirm();
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<ModelProviderDraft>(emptyDraft);
  const [savedSnapshot, setSavedSnapshot] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [newModelId, setNewModelId] = useState("");
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [remoteDetails, setRemoteDetails] = useState<RemoteModelDetails>({});
  const [remoteSelected, setRemoteSelected] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    const rows = await translateApi.modelProviders();
    setProviders(rows);
    return rows;
  }, []);

  function selectProvider(id: string, rows = providers) {
    const provider = rows.find((item) => item.id === id);
    const next = provider ? toDraft(provider) : emptyDraft();
    setSelectedId(id);
    setDraft(next);
    setSavedSnapshot(provider ? JSON.stringify(next) : "");
    setSelectedModel(next.models[0]?.id ?? "");
    setNewModelId("");
    setRemoteModels([]);
    setRemoteDetails({});
    setRemoteSelected("");
    setError("");
    setMessage("");
  }

  useEffect(() => {
    void refresh()
      .then((rows) => {
        const provider = rows[0];
        if (!provider) return;
        const next = toDraft(provider);
        setSelectedId(provider.id);
        setDraft(next);
        setSavedSnapshot(JSON.stringify(next));
        setSelectedModel(next.models[0]?.id ?? "");
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [refresh]);

  const selectedProvider = providers.find((item) => item.id === selectedId) ?? null;
  const isNew = selectedId === "_new";
  const isIndex = selectedProvider?.api_type === "index_translate";
  const isCompatibility = selectedProvider?.kind === "compatibility";
  const isCustom = isNew || selectedProvider?.kind === "custom";
  const model = draft.models.find((item) => item.id === selectedModel) ?? null;
  const dirty = isNew
    ? Boolean(draft.name || draft.models.length)
    : JSON.stringify(draft) !== savedSnapshot;

  function updateModel(patch: Partial<ProviderModel>) {
    setDraft((current) => ({
      ...current,
      models: current.models.map((item) =>
        item.id === selectedModel ? { ...item, ...patch } : item
      ),
    }));
    if (patch.id) setSelectedModel(patch.id);
  }

  function addModel(
    idValue: string,
    reported?: { context_window: number | null; max_output_tokens: number | null },
  ) {
    const id = idValue.trim();
    if (!id || id.length > 256) return;
    setDraft((current) => {
      if (current.models.some((item) => item.id === id)) return current;
      return { ...current, models: [...current.models, newModel(id, reported)] };
    });
    setSelectedModel(id);
    setNewModelId("");
  }

  function removeModel(id: string) {
    setDraft((current) => {
      const models = current.models.filter((item) => item.id !== id);
      setSelectedModel(models[0]?.id ?? "");
      return { ...current, models };
    });
  }

  function updateIndexOptions(patch: Record<string, unknown>) {
    setDraft((current) => ({
      ...current,
      options: { ...current.options, ...patch },
    }));
  }

  async function save() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      validateProviderDraft(draft, { singleModel: Boolean(isCompatibility || isIndex) });
      const saved = await translateApi.saveModelProvider(isNew ? null : selectedId, draft);
      const rows = await refresh();
      selectProvider(saved.id, rows);
      setMessage("模型服务已保存");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  async function removeProvider() {
    if (!selectedProvider?.deletable) return;
    const ok = await confirm({
      title: "删除模型服务",
      message: "删除后，仍引用该服务且尚未执行的任务将无法解析模型。已完成任务不受影响。",
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;

    setLoading(true);
    setError("");
    try {
      await translateApi.removeModelProvider(selectedProvider.id);
      const rows = await refresh();
      selectProvider(rows[0]?.id ?? "_new", rows);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  async function pullModels() {
    if (!selectedProvider?.supports_remote_catalog) return;
    setLoading(true);
    setError("");
    try {
      const result = await translateApi.remoteProviderModels(selectedProvider.id);
      setRemoteModels(result.models);
      setRemoteDetails(result.details ?? {});
      setRemoteSelected(result.models[0] ?? "");
      setMessage(result.models.length ? `接口返回 ${result.models.length} 个模型` : "接口没有返回可用模型");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Section>
      <div className="flex flex-col gap-3 border-b border-slate-200 pb-4 dark:border-slate-800 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-sm font-semibold text-slate-950 dark:text-white">模型服务</div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            在这里管理可用接口和模型；自动任务实际使用哪个模型，在“自动处理”里单独选择。
          </div>
        </div>
        <Button size="xs" onClick={() => selectProvider("_new")}>添加模型服务</Button>
      </div>

      <div className="mt-4 grid gap-5 lg:grid-cols-[220px_minmax(0,1fr)]">
        <ModelProviderSidebar
          providers={providers}
          selectedId={selectedId}
          isNew={isNew}
          onSelect={selectProvider}
        />

        <div className="min-w-0 space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-base font-semibold text-slate-950 dark:text-white">
                {isNew ? "新建模型服务" : selectedProvider?.name ?? "模型服务"}
              </div>
              {selectedProvider ? (
                <div className="mt-1 text-xs text-slate-500">
                  {selectedProvider.kind === "compatibility"
                    ? "兼容历史任务的内建 OpenAI 配置；仍使用统一的模型服务编辑入口。"
                    : selectedProvider.kind === "builtin"
                      ? "VideoRoll 内建服务，不能删除。"
                      : "独立保存的 OpenAI 兼容模型服务。"}
                </div>
              ) : null}
            </div>
            <div className="flex gap-2">
              {selectedProvider?.deletable ? (
                <Button size="xs" tone="danger" disabled={loading} onClick={() => void removeProvider()}>
                  删除
                </Button>
              ) : null}
              <Button size="xs" tone="primary" disabled={loading || !dirty} onClick={() => void save()}>
                {loading ? "保存中…" : "保存模型服务"}
              </Button>
            </div>
          </div>

          <ModelProviderConnectionForm
            provider={selectedProvider}
            draft={draft}
            isCustom={Boolean(isCustom)}
            isCompatibility={Boolean(isCompatibility)}
            isIndex={Boolean(isIndex)}
            onChange={setDraft}
          />

          <ModelProviderModelsEditor
            provider={selectedProvider}
            draft={draft}
            model={model}
            selectedModel={selectedModel}
            newModelId={newModelId}
            remoteModels={remoteModels}
            remoteDetails={remoteDetails}
            remoteSelected={remoteSelected}
            loading={loading}
            isCustom={Boolean(isCustom)}
            isIndex={Boolean(isIndex)}
            isCompatibility={Boolean(isCompatibility)}
            onSelectedModelChange={setSelectedModel}
            onNewModelIdChange={setNewModelId}
            onRemoteSelectedChange={setRemoteSelected}
            onPullModels={() => void pullModels()}
            onAddModel={addModel}
            onUpdateModel={updateModel}
            onRemoveModel={removeModel}
          />

          {isIndex ? (
            <IndexTranslateOptions draft={draft} onOptionsChange={updateIndexOptions} />
          ) : null}

          {error ? <InlineNotice tone="danger">{error}</InlineNotice> : null}
          {message ? <InlineNotice tone="success">{message}</InlineNotice> : null}
        </div>
      </div>
    </Section>
  );
}
