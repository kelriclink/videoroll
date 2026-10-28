import type { ModelProvider } from "../api/translate";

type Props = {
  providers: ModelProvider[];
  providerId: string;
  modelId: string;
  onChange: (providerId: string, modelId: string) => void;
  disabled?: boolean;
};

export function ModelChoiceSelect({ providers, providerId, modelId, onChange, disabled }: Props) {
  // Index-Translate is selected through the translation-engine selector, not
  // as an OpenAI-compatible provider: it uses a distinct transport.
  const compatibleProviders = providers.filter((item) => item.api_type !== "index_translate");
  const active = compatibleProviders.find((item) => item.id === providerId);
  const models = active?.models ?? [];
  const modelExists = models.some((model) => model.id === modelId);
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <label className="block text-xs text-slate-600">
        模型提供方
        <select aria-label="模型提供方" disabled={disabled || !compatibleProviders.length}
          className="mt-1 w-full rounded border px-3 py-2 text-sm text-slate-900"
          value={providerId} onChange={(event) => {
            const id = event.target.value;
            const next = compatibleProviders.find((item) => item.id === id);
            onChange(id, id === "legacy" ? "" : (next?.models[0]?.id ?? ""));
          }}>
          {compatibleProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
        </select>
      </label>
      <label className="block text-xs text-slate-600">
        翻译模型
        <select aria-label="翻译模型" disabled={disabled || !active}
          className="mt-1 w-full rounded border px-3 py-2 text-sm text-slate-900"
          value={modelId || (providerId === "legacy" ? models[0]?.id ?? "" : "")}
          onChange={(event) => onChange(providerId, event.target.value)}>
          {!modelExists && providerId !== "legacy" && <option value="">请选择模型</option>}
          {models.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
        </select>
      </label>
      <p className="text-xs text-slate-500 sm:col-span-2">模型和参数在「翻译 / RAG → 模型提供方」中管理。未选择新提供方时仍使用现有 OpenAI 配置。</p>
    </div>
  );
}
