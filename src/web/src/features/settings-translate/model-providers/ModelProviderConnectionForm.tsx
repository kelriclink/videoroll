import type { ModelProvider, ModelProviderDraft } from "../../../api/translate";
import { InlineNotice } from "../../../components/ui";
import { inputClass, labelClass } from "./modelProviderForm";

export function ModelProviderConnectionForm({
  provider,
  draft,
  isCustom,
  isCompatibility,
  isIndex,
  onChange,
}: {
  provider: ModelProvider | null;
  draft: ModelProviderDraft;
  isCustom: boolean;
  isCompatibility: boolean;
  isIndex: boolean;
  onChange: (next: ModelProviderDraft) => void;
}) {
  if (isIndex) {
    return (
      <InlineNotice title="Index-Translate 官方免费服务">
        接口地址和模型由 VideoRoll 固定管理，无需 API 密钥。下方只保留会实际影响翻译结果的参数。
      </InlineNotice>
    );
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <label className={labelClass}>
        <span>服务名称</span>
        <input
          className={inputClass}
          disabled={!isCustom}
          value={draft.name}
          onChange={(event) => onChange({ ...draft, name: event.target.value })}
          placeholder="例如：本地 llama.cpp"
        />
      </label>
      <label className={labelClass}>
        <span>接口类型</span>
        <select
          className={inputClass}
          disabled={isCompatibility ? false : !isCustom}
          value={draft.api_type}
          onChange={(event) => onChange({ ...draft, api_type: event.target.value as "openai" | "cerebras" })}
        >
          <option value="openai">OpenAI 兼容</option>
          <option value="cerebras">Cerebras</option>
        </select>
      </label>
      <label className={labelClass + " sm:col-span-2"}>
        <span>API 地址</span>
        <input
          className={inputClass}
          disabled={!isCustom && !isCompatibility}
          value={draft.base_url}
          onChange={(event) => onChange({ ...draft, base_url: event.target.value })}
          placeholder="https://api.example.com/v1"
        />
      </label>
      <label className={labelClass + " sm:col-span-2"}>
        <span>API 密钥</span>
        <input
          type="password"
          autoComplete="new-password"
          className={inputClass}
          value={draft.api_key ?? ""}
          onChange={(event) => onChange({ ...draft, api_key: event.target.value })}
          placeholder={provider?.api_key_set ? "已设置；留空保持不变" : "可留空"}
        />
      </label>
      <label className={labelClass}>
        <span>请求超时（秒）</span>
        <input
          className={inputClass}
          type="number"
          min={5}
          max={1200}
          value={draft.timeout_seconds}
          onChange={(event) => onChange({ ...draft, timeout_seconds: Number(event.target.value) })}
        />
      </label>
      <label className={labelClass}>
        <span>网络失败重试次数</span>
        <input
          className={inputClass}
          type="number"
          min={1}
          max={10}
          value={draft.max_retries}
          onChange={(event) => onChange({ ...draft, max_retries: Number(event.target.value) })}
        />
      </label>
    </div>
  );
}
