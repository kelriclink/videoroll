import type { ModelProvider, ModelProviderDraft, ProviderModel } from "../../../api/translate";
import { Button, TechnicalDetails } from "../../../components/ui";
import { inputClass, labelClass, type RemoteModelDetails } from "./modelProviderForm";

export function ModelProviderModelsEditor({
  provider,
  draft,
  model,
  selectedModel,
  newModelId,
  remoteModels,
  remoteDetails,
  remoteSelected,
  loading,
  isCustom,
  isIndex,
  isCompatibility,
  onSelectedModelChange,
  onNewModelIdChange,
  onRemoteSelectedChange,
  onPullModels,
  onAddModel,
  onUpdateModel,
  onRemoveModel,
}: {
  provider: ModelProvider | null;
  draft: ModelProviderDraft;
  model: ProviderModel | null;
  selectedModel: string;
  newModelId: string;
  remoteModels: string[];
  remoteDetails: RemoteModelDetails;
  remoteSelected: string;
  loading: boolean;
  isCustom: boolean;
  isIndex: boolean;
  isCompatibility: boolean;
  onSelectedModelChange: (value: string) => void;
  onNewModelIdChange: (value: string) => void;
  onRemoteSelectedChange: (value: string) => void;
  onPullModels: () => void;
  onAddModel: (
    id: string,
    reported?: { context_window: number | null; max_output_tokens: number | null },
  ) => void;
  onUpdateModel: (patch: Partial<ProviderModel>) => void;
  onRemoveModel: (id: string) => void;
}) {
  return (
    <div className="border-t border-slate-200 pt-4 dark:border-slate-800">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-950 dark:text-white">模型</div>
          <div className="mt-1 text-xs text-slate-500">
            {isCustom ? "可为一个服务配置多个模型。" : "内建服务只保留一个模型。"}
          </div>
        </div>
        {provider?.supports_remote_catalog ? (
          <Button size="xs" disabled={loading} onClick={onPullModels}>
            从接口读取模型
          </Button>
        ) : null}
      </div>

      {isCustom ? (
        <div className="mb-3 flex flex-wrap gap-2">
          <input
            className="vr-input min-w-0 flex-1"
            value={newModelId}
            onChange={(event) => onNewModelIdChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onAddModel(newModelId);
            }}
            placeholder="手动输入模型 ID"
          />
          <Button size="xs" onClick={() => onAddModel(newModelId)}>添加</Button>
        </div>
      ) : null}

      {remoteModels.length > 0 ? (
        <div className="mb-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div className="flex flex-col gap-2 sm:flex-row">
            <select
              className="vr-input min-w-0 flex-1"
              value={remoteSelected}
              onChange={(event) => onRemoteSelectedChange(event.target.value)}
            >
              {remoteModels.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <Button
              size="xs"
              onClick={() => onAddModel(remoteSelected, remoteDetails[remoteSelected])}
            >
              加入模型列表
            </Button>
          </div>
          {remoteSelected ? (
            <div className="mt-2 text-xs text-slate-500">
              上下文：{remoteDetails[remoteSelected]?.context_window?.toLocaleString() ?? "接口未声明"}
              {" · "}
              最大输出：{remoteDetails[remoteSelected]?.max_output_tokens?.toLocaleString() ?? "接口未声明"}
            </div>
          ) : null}
        </div>
      ) : null}

      {draft.models.length > 1 ? (
        <div className="mb-3 flex flex-wrap gap-1">
          {draft.models.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelectedModelChange(item.id)}
              className={[
                "rounded-lg px-2.5 py-1.5 text-xs",
                selectedModel === item.id
                  ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                  : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
              ].join(" ")}
            >
              {item.id}
            </button>
          ))}
        </div>
      ) : null}

      {model ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <div className="truncate text-sm font-medium">{model.id}</div>
            {isCustom && draft.models.length > 1 ? (
              <button
                type="button"
                className="text-xs text-rose-700 dark:text-rose-300"
                onClick={() => onRemoveModel(model.id)}
              >
                移除模型
              </button>
            ) : null}
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <label className={labelClass}>
              <span>模型 ID</span>
              <input
                className={inputClass}
                disabled={isIndex}
                value={model.id}
                onChange={(event) => onUpdateModel({ id: event.target.value })}
              />
            </label>
            <label className={labelClass}>
              <span>温度</span>
              <input
                className={inputClass}
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={model.temperature ?? ""}
                onChange={(event) => onUpdateModel({
                  temperature: event.target.value ? Number(event.target.value) : null,
                })}
              />
            </label>
            <label className={labelClass}>
              <span>最大输出 Token</span>
              <input
                className={inputClass}
                type="number"
                min={1}
                disabled={isCompatibility}
                value={model.max_output_tokens ?? ""}
                onChange={(event) => onUpdateModel({
                  max_output_tokens: event.target.value ? Number(event.target.value) : null,
                })}
                placeholder={isCompatibility ? "兼容模式不保存容量" : "使用上游默认"}
              />
            </label>
          </div>

          {!isIndex ? (
            <TechnicalDetails summary="模型高级参数">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className={labelClass}>
                  <span>上下文窗口 Token</span>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    disabled={isCompatibility}
                    value={model.context_window ?? ""}
                    onChange={(event) => onUpdateModel({
                      context_window: event.target.value ? Number(event.target.value) : null,
                    })}
                    placeholder={isCompatibility ? "兼容模式不保存容量" : "未知"}
                  />
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={model.enable_thinking}
                    onChange={(event) => onUpdateModel({ enable_thinking: event.target.checked })}
                  />
                  启用模型推理
                </label>
                {draft.api_type === "cerebras" ? (
                  <>
                    <label className={labelClass}>
                      <span>推理强度</span>
                      <select
                        className={inputClass}
                        value={model.reasoning_effort}
                        onChange={(event) => onUpdateModel({
                          reasoning_effort: event.target.value as ProviderModel["reasoning_effort"],
                        })}
                      >
                        <option value="low">低</option>
                        <option value="medium">中</option>
                        <option value="high">高</option>
                      </select>
                    </label>
                    <label className={labelClass}>
                      <span>推理输出</span>
                      <select
                        className={inputClass}
                        value={model.reasoning_format}
                        onChange={(event) => onUpdateModel({
                          reasoning_format: event.target.value as ProviderModel["reasoning_format"],
                        })}
                      >
                        <option value="parsed">分离显示</option>
                        <option value="hidden">隐藏</option>
                        <option value="raw">混入原始响应</option>
                      </select>
                    </label>
                  </>
                ) : null}
              </div>
            </TechnicalDetails>
          ) : null}
        </div>
      ) : (
        <div className="py-5 text-sm text-slate-500">尚未配置模型。</div>
      )}
    </div>
  );
}
