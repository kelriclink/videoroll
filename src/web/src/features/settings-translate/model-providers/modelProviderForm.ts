import type { ModelProvider, ModelProviderDraft, ProviderModel } from "../../../api/translate";

export const inputClass = "vr-input w-full";
export const labelClass = "block space-y-1 text-xs font-medium text-slate-600 dark:text-slate-300";

export type RemoteModelDetails = Record<
  string,
  { context_window: number | null; max_output_tokens: number | null }
>;

export function emptyDraft(): ModelProviderDraft {
  return {
    name: "",
    base_url: "https://api.openai.com/v1",
    api_type: "openai",
    models: [],
    timeout_seconds: 180,
    max_retries: 3,
    api_key: "",
    options: {},
  };
}

export function toDraft(provider: ModelProvider): ModelProviderDraft {
  return {
    name: provider.name,
    base_url: provider.base_url,
    api_type: provider.api_type,
    models: provider.models.map((model) => ({ ...model })),
    timeout_seconds: provider.timeout_seconds,
    max_retries: provider.max_retries,
    options: { ...(provider.options ?? {}) },
  };
}

export function newModel(
  id: string,
  reported?: { context_window: number | null; max_output_tokens: number | null },
): ProviderModel {
  return {
    id,
    context_window: reported?.context_window ?? null,
    max_output_tokens: reported?.max_output_tokens ?? null,
    temperature: 0.2,
    enable_thinking: false,
    reasoning_effort: "medium",
    reasoning_format: "parsed",
  };
}

export function providerKindLabel(provider: ModelProvider) {
  if (provider.kind === "builtin") return "内建";
  if (provider.kind === "compatibility") return "兼容";
  return "自定义";
}

export function validateProviderDraft(
  draft: ModelProviderDraft,
  options: { singleModel: boolean },
): void {
  if (!draft.models.length) throw new Error("至少需要配置一个模型");
  if (options.singleModel && draft.models.length !== 1) {
    throw new Error("内建翻译服务只能配置一个模型");
  }
  for (const item of draft.models) {
    for (const value of [item.context_window, item.max_output_tokens]) {
      if (value !== null && value !== undefined && (!Number.isSafeInteger(value) || value <= 0)) {
        throw new Error(`${item.id}：Token 数必须是正整数且能被精确保存`);
      }
    }
    if (
      item.context_window != null
      && item.max_output_tokens != null
      && item.max_output_tokens >= item.context_window
    ) {
      throw new Error(`${item.id}：最大输出 Token 必须小于上下文窗口`);
    }
  }
}

export function indexOptionsFromDraft(draft: ModelProviderDraft) {
  return {
    batchSize: Number(draft.options.batch_size ?? 12),
    contextChars: Number(draft.options.context_chars ?? 6000),
    domain: String(draft.options.domain ?? "视频字幕"),
    glossary: String(draft.options.glossary ?? ""),
    instruction: String(draft.options.instruction ?? ""),
  };
}
