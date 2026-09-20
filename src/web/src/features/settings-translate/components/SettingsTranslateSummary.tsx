import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Section } from "../../../components/ui";
export function SettingsTranslateSummary({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    agentSkills
  } = controller;
  const statusClass = (enabled: boolean) =>
    enabled ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500";
  return (
    <Section>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-sm font-semibold text-slate-900">运行配置</div>
            <div className="mt-1 text-xs text-slate-500">只展示影响当前翻译链的关键状态；详细参数在下方各页修改。</div>
          </div>
          <div className="text-xs text-slate-500">Agent Skills：{agentSkills.length}</div>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中...</div>
        ) : (
          <div className="mt-3 grid gap-3 lg:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-lg border border-slate-200 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-slate-500">Translator</div>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusClass(settings.openai_api_key_set || settings.default_provider !== "openai")}`}>
                  {settings.openai_api_key_set || settings.default_provider !== "openai" ? "Ready" : "Key missing"}
                </span>
              </div>
              <div className="mt-2 truncate text-sm font-medium text-slate-900" title={settings.openai_model}>{settings.openai_model}</div>
              <div className="mt-1 text-xs text-slate-500">{settings.default_target_lang} · batch {settings.default_batch_size}</div>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-slate-500">RAG</div>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusClass(settings.rag_enabled)}`}>
                  {settings.rag_enabled ? "Enabled" : "Disabled"}
                </span>
              </div>
              <div className="mt-2 text-sm font-medium text-slate-900">Top {settings.rag_top_k} · score {settings.rag_min_score}</div>
              <div className="mt-1 text-xs text-slate-500">
                {settings.rag_dictionary_enabled ? "Dictionary" : "No dictionary"} · {settings.rag_search_enabled ? "Web search" : "No web search"}
              </div>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-slate-500">Embedding</div>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusClass(settings.rag_embedding_provider !== "openai" || settings.rag_embedding_api_key_set)}`}>
                  {settings.rag_embedding_dimensions}d
                </span>
              </div>
              <div className="mt-2 truncate text-sm font-medium text-slate-900" title={settings.rag_embedding_model}>{settings.rag_embedding_model}</div>
              <div className="mt-1 text-xs text-slate-500">{settings.rag_embedding_provider} · {settings.rag_embedding_device}</div>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-slate-500">Agent</div>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${statusClass(settings.rag_agent_skills_enabled)}`}>
                  {settings.rag_agent_skills_enabled ? "Skills on" : "Skills off"}
                </span>
              </div>
              <div className="mt-2 text-sm font-medium text-slate-900">{settings.rag_agent_parallelism} 并发 · {settings.rag_agent_timeout_seconds}s</div>
              <div className="mt-1 text-xs text-slate-500">{agentSkills.length} skills available</div>
            </div>
          </div>
        )}
      </Section>
  );
}
