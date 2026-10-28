import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  translateApi, type ModelProvider, type ModelProviderDraft, type ProviderModel,
} from "../../../api/translate";

const emptyDraft = (): ModelProviderDraft => ({
  name: "", base_url: "https://api.openai.com/v1", api_type: "openai",
  models: [], timeout_seconds: 180, max_retries: 3, api_key: "",
});
const modelInputClass = "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900";
const fieldLabelClass = "block space-y-1 text-xs font-medium text-slate-600";

function toDraft(provider: ModelProvider): ModelProviderDraft {
  return {
    name: provider.name, base_url: provider.base_url, api_type: provider.api_type,
    models: provider.models.map((model) => ({ ...model })),
    timeout_seconds: provider.timeout_seconds, max_retries: provider.max_retries,
  };
}

export function ModelProvidersWorkspace() {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [selectedId, setSelectedId] = useState("legacy");
  const [draft, setDraft] = useState<ModelProviderDraft>(emptyDraft);
  const [selectedModel, setSelectedModel] = useState("");
  const [newModel, setNewModel] = useState("");
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [remoteSelected, setRemoteSelected] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    const rows = await translateApi.modelProviders();
    setProviders(rows);
    return rows;
  }, []);

  useEffect(() => {
    void refresh()
      .then((rows) => {
        const legacy = rows.find((item) => item.id === "legacy");
        if (legacy) {
          setDraft(toDraft(legacy));
          setSelectedModel(legacy.models[0]?.id ?? "");
        }
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [refresh]);

  function choose(id: string, rows = providers) {
    setSelectedId(id);
    const provider = rows.find((item) => item.id === id);
    const next = provider ? toDraft(provider) : emptyDraft();
    setDraft(next);
    setSelectedModel(next.models[0]?.id ?? "");
    setRemoteModels([]);
    setRemoteSelected("");
    setNewModel("");
    setMessage("");
    setError("");
  }

  function updateModel(field: keyof ProviderModel, value: number | null) {
    setDraft((previous) => ({
      ...previous,
      models: previous.models.map((item) => item.id === selectedModel ? { ...item, [field]: value } : item),
    }));
  }

  function addModel(value: string) {
    const id = value.trim();
    if (!id || id.length > 256) return;
    setDraft((previous) => previous.models.some((item) => item.id === id) ? previous : ({
      ...previous,
      models: [...previous.models, { id, context_window: null, max_output_tokens: null, temperature: 0.2, enable_thinking: false, reasoning_effort: "medium" }],
    }));
    setSelectedModel(id);
    setNewModel("");
  }

  async function save() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      if (!draft.models.length) throw new Error("至少需要添加一个模型，才可以保存提供方");
      const saved = await translateApi.saveModelProvider(selectedId === "_new" ? null : selectedId, draft);
      const rows = await refresh();
      choose(saved.id, rows);
      setMessage("提供方及模型参数已保存");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }

  async function removeProvider() {
    if (selectedId === "legacy" || selectedId === "_new") return;
    if (!window.confirm("删除此提供方后，使用它的未执行任务将无法解析模型。确定删除？")) return;
    setLoading(true);
    try {
      await translateApi.removeModelProvider(selectedId);
      const rows = await refresh();
      choose("legacy", rows);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }

  async function pullModels() {
    if (selectedId === "legacy" || selectedId === "_new") return;
    setLoading(true);
    setError("");
    try {
      const result = await translateApi.remoteProviderModels(selectedId);
      setRemoteModels(result.models);
      setRemoteSelected(result.models[0] ?? "");
      setMessage("接口返回 " + result.models.length + " 个模型，可按需添加到当前提供方");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }

  const legacy = selectedId === "legacy";
  const indexProvider = selectedId === "index_translate";
  const builtIn = legacy || indexProvider;
  const model = draft.models.find((item) => item.id === selectedModel);

  return (
    <section className="overflow-hidden rounded-xl border border-slate-700 bg-slate-950 text-slate-100">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-700 px-5 py-4">
        <div>
          <h2 className="text-lg font-semibold">模型提供方</h2>
          <p className="mt-1 text-xs text-slate-400">集中管理 OpenAI 兼容接口、密钥与模型；手动/自动任务独立选择，不再暗中复用一个模型</p>
        </div>
        <button type="button" className="rounded-lg border border-blue-500 px-3 py-2 text-sm text-blue-300 hover:bg-slate-800" onClick={() => choose("_new")}>＋ 添加提供方</button>
      </div>
      <div className="grid min-h-[420px] lg:grid-cols-[210px_250px_minmax(0,1fr)]">
        <aside className="space-y-2 border-b border-slate-700 p-3 lg:border-b-0 lg:border-r">
          <div className="px-2 pb-1 text-xs font-semibold uppercase tracking-wider text-slate-400">提供方</div>
          {providers.map((provider) => (
            <button key={provider.id} type="button" onClick={() => choose(provider.id)}
              className={"flex w-full items-center justify-between rounded-lg px-3 py-3 text-left text-sm " +
                (selectedId === provider.id ? "bg-blue-900/60 text-blue-200" : "hover:bg-slate-800")}>
              <span className="truncate">{provider.name}</span>
              <span className={"ml-2 h-2 w-2 shrink-0 rounded-full " + (provider.api_key_set ? "bg-emerald-400" : "bg-slate-600")} />
            </button>
          ))}
          {selectedId === "_new" && <div className="rounded-lg bg-blue-900/60 px-3 py-3 text-sm text-blue-100">新提供方（未保存）</div>}
        </aside>
        <div className="border-b border-slate-700 p-3 lg:border-b-0 lg:border-r">
          <div className="mb-3 flex items-center justify-between gap-2 px-1">
            <span className="text-sm font-semibold">模型列表</span>
            <span className="text-xs text-slate-400">{draft.models.length} 个</span>
          </div>
          {draft.models.map((item) => (
            <button key={item.id} type="button" onClick={() => setSelectedModel(item.id)}
              className={"mb-1 block w-full truncate rounded-lg px-3 py-3 text-left text-sm " +
                (selectedModel === item.id ? "bg-blue-900/60 text-blue-100" : "hover:bg-slate-800")}>
              {item.id}
              {item.context_window ? <span className="block text-xs text-slate-400">{item.context_window.toLocaleString()} Token</span> : null}
            </button>
          ))}
          {builtIn && <p className="mt-3 text-xs text-slate-400">{indexProvider ? "Index 参数在下方专用区域管理。" : "现有配置由下方旧版高级设置管理。"}</p>}
          {!builtIn && (
            <div className="mt-3 space-y-2 border-t border-slate-700 pt-3">
              <label className="block text-xs text-slate-400">手动添加模型 ID</label>
              <div className="flex gap-1">
                <input aria-label="模型 ID" className="min-w-0 flex-1 rounded border border-slate-600 bg-slate-800 px-2 py-2 text-sm" value={newModel} onChange={(e) => setNewModel(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") addModel(newModel); }} />
                <button type="button" className="rounded bg-blue-700 px-2 text-sm" onClick={() => addModel(newModel)}>添加</button>
              </div>
              <button type="button" disabled={loading || selectedId === "_new"} className="w-full rounded border border-slate-600 px-2 py-2 text-xs hover:bg-slate-800 disabled:opacity-40" onClick={() => void pullModels()}>从接口获取模型</button>
              {remoteModels.length > 0 && <div className="space-y-2">
                <select aria-label="远程模型" className="w-full rounded bg-slate-800 px-2 py-2 text-xs" value={remoteSelected} onChange={(e) => setRemoteSelected(e.target.value)}>
                  {remoteModels.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
                <button type="button" className="w-full rounded bg-blue-700 px-2 py-2 text-xs" onClick={() => addModel(remoteSelected)}>加入模型列表</button>
              </div>}
            </div>
          )}
        </div>
        <div className="space-y-4 p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-base font-semibold">{legacy ? "现有 OpenAI 接口" : indexProvider ? "Index-Translate 官方接口" : selectedId === "_new" ? "新建提供方" : "编辑提供方"}</div>
            {!builtIn && <div className="flex gap-2">
              {selectedId !== "_new" && <button type="button" onClick={() => void removeProvider()} className="rounded border border-rose-400/60 px-3 py-2 text-xs text-rose-300">删除提供方</button>}
              <button type="button" disabled={loading} onClick={() => void save()} className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50">保存提供方</button>
            </div>}
          </div>
          {legacy ? <div className="rounded-lg border border-slate-700 bg-slate-900 p-4 text-sm text-slate-300">
            该项直接读取现有 OpenAI 配置，兼容历史任务。若要修改原有接口，请展开本页下方的“旧版接口高级设置”；
            也可以点击「添加提供方」新建多个独立接口。
            <div className="mt-3"><Link className="text-blue-300 underline" to="/settings/auto">在自动模式页面选择默认模型</Link></div>
          </div> : indexProvider ? <div className="rounded-lg border border-slate-700 bg-slate-900 p-4 text-sm text-slate-300">
            <p>Index-Translate-35B-A3B 通过独立的官方免费接口调用，不依赖 OpenAI API Key，也不应通过 OpenAI 的 /models 接口获取。</p>
            <p className="mt-2">旧参数（每批字幕数、最大输出 Token、温度、上下文参考字符数、术语和超时）均保留。可在本页下方单独编辑，不需要切换全局默认 Provider。</p>
            <a className="mt-3 inline-block text-blue-300 underline" href="#index-translate-config">查看和修改 Index-Translate 参数</a>
          </div> : <>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className={fieldLabelClass}>提供方名称<input className={modelInputClass} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="例如 Ollama / SiliconFlow / New API" /></label>
              <label className={fieldLabelClass}>接口类型<select className={modelInputClass} value={draft.api_type} onChange={(e) => setDraft({ ...draft, api_type: e.target.value as "openai" | "cerebras" })}><option value="openai">OpenAI 兼容</option><option value="cerebras">Cerebras</option></select></label>
              <label className={fieldLabelClass + " sm:col-span-2"}>API 主机（包含 /v1）<input className={modelInputClass} value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
              <label className={fieldLabelClass + " sm:col-span-2"}>API 密钥<input type="password" autoComplete="new-password" className={modelInputClass} value={draft.api_key ?? ""} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} placeholder="留空保留已有密钥；新建时可为空" /></label>
              <label className={fieldLabelClass}>请求超时（秒）<input type="number" min={5} max={1200} className={modelInputClass} value={draft.timeout_seconds} onChange={(e) => setDraft({ ...draft, timeout_seconds: Number(e.target.value) })} /></label>
              <label className={fieldLabelClass}>最大重试次数<input type="number" min={1} max={10} className={modelInputClass} value={draft.max_retries} onChange={(e) => setDraft({ ...draft, max_retries: Number(e.target.value) })} /></label>
            </div>
            {model && <div className="space-y-3 border-t border-slate-700 pt-4">
              <div className="flex items-center justify-between gap-2"><div className="font-semibold">模型：{model.id}</div>
                <button type="button" className="text-xs text-rose-300" onClick={() => { setDraft({ ...draft, models: draft.models.filter((item) => item.id !== model.id) }); setSelectedModel(draft.models.find((item) => item.id !== model.id)?.id ?? ""); }}>移除模型</button></div>
              <div className="grid gap-3 sm:grid-cols-3">
                <label className={fieldLabelClass}>上下文窗口 Token<input className={modelInputClass} type="number" min={1024} placeholder="未知（手动填写）" value={model.context_window ?? ""} onChange={(e) => updateModel("context_window", e.target.value ? Number(e.target.value) : null)} /></label>
                <label className={fieldLabelClass}>最大输出 Token<input className={modelInputClass} type="number" min={1} placeholder="上游默认" value={model.max_output_tokens ?? ""} onChange={(e) => updateModel("max_output_tokens", e.target.value ? Number(e.target.value) : null)} /></label>
                <label className={fieldLabelClass}>温度<input className={modelInputClass} type="number" min={0} max={2} step={0.1} value={model.temperature ?? ""} onChange={(e) => updateModel("temperature", e.target.value ? Number(e.target.value) : null)} /></label>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={model.enable_thinking ?? false}
                  onChange={(event) => setDraft({ ...draft, models: draft.models.map((item) =>
                    item.id === model.id ? { ...item, enable_thinking: event.target.checked } : item) })} />启用 Thinking（仅兼容上游）</label>
                {draft.api_type === "cerebras" && <label className={fieldLabelClass}>推理强度
                  <select className={modelInputClass} value={model.reasoning_effort ?? "medium"} onChange={(event) =>
                    setDraft({ ...draft, models: draft.models.map((item) =>
                      item.id === model.id ? { ...item, reasoning_effort: event.target.value as "low" | "medium" | "high" } : item) })}>
                    <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
                  </select>
                </label>}
              </div>
              <p className="text-xs text-slate-400">上下文窗口不会根据模型名称猜测；请填写实际上游支持的值。最大输出用于每次生成请求，而不是模型总上下文。</p>
            </div>}
          </>}
          {error && <div role="alert" className="rounded bg-rose-950/50 p-3 text-sm text-rose-200">{error}</div>}
          {message && <div role="status" className="rounded bg-emerald-950/40 p-3 text-sm text-emerald-200">{message}</div>}
        </div>
      </div>
    </section>
  );
}
