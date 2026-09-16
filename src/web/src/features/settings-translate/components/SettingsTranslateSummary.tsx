import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Section } from "../../../components/ui";
export function SettingsTranslateSummary({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    agentSkills
  } = controller;
  return (
<Section>
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-slate-900">当前配置</div>
          <div className="text-xs text-slate-500">
            OpenAI API Key：{settings?.openai_api_key_set ? <span className="text-emerald-700">已设置</span> : <span className="text-rose-700">未设置</span>}
          </div>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中...</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            {[
              ["provider", settings.default_provider],
              ["target", settings.default_target_lang],
              ["batch", settings.default_batch_size],
              ["summary", settings.default_enable_summary ? "true" : "false"],
              ["API type", settings.openai_api_type === "cerebras" ? "Cerebras" : "OpenAI compatible"],
              ["think", settings.openai_enable_thinking ? "enabled" : "disabled"],
              ["rag", settings.rag_enabled ? "enabled" : "disabled"],
              ["dictionary", settings.rag_dictionary_enabled ? `${settings.rag_dictionary_top_k} / ${settings.rag_dictionary_min_quality}` : "disabled"],
              ["wiki", settings.rag_wiki_enabled ? "enabled" : "disabled"],
              ["search", settings.rag_search_enabled ? `${settings.rag_search_categories || "general"} / ${settings.rag_search_language || "all"}` : "disabled"],
              ["agents", `${settings.rag_agent_parallelism} / ${settings.rag_agent_timeout_seconds}s`],
              ["skills", settings.rag_agent_skills_enabled ? `${agentSkills.length} available` : "disabled"],
              ["embedding", `${settings.rag_embedding_provider}:${settings.rag_embedding_model}`],
              ["embedding key", settings.rag_embedding_api_key_set ? "set" : "unset"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-md border border-slate-200 p-3">
                <div className="text-xs text-slate-500">{label}</div>
                <div className="mt-1 break-all font-mono text-sm text-slate-900">{value}</div>
              </div>
            ))}
          </div>
        )}
      </Section>
  );
}
