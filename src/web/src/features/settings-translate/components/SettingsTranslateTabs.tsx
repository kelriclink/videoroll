import type { SettingsTranslateController } from "../useSettingsTranslateController";
import type { TranslateSettingsTab } from "../form";

export function SettingsTranslateTabs({ controller }: { controller: SettingsTranslateController }) {
  const {
    activeTab,
    setActiveTab
  } = controller;
  const tabs: Array<[TranslateSettingsTab, string, string]> = [
    ["translation", "翻译模型", "模型与默认翻译参数"],
    ["rag", "RAG Agent", "研究、词典与搜索"],
    ["embedding", "Embedding", "向量模型与知识库"],
    ["test", "诊断", "翻译与连接测试"],
  ];
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white p-1 shadow-sm">
        <div className="flex min-w-max gap-1">
          {tabs.map(([id, label, description]) => (
            <button
              key={id}
              type="button"
              aria-pressed={activeTab === id}
              onClick={() => setActiveTab(id)}
              className={[
                "min-w-32 rounded-lg px-3 py-2 text-left transition-colors",
                activeTab === id
                  ? "bg-slate-900 text-white"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-950",
              ].join(" ")}
            >
              <span className="block text-sm font-medium">{label}</span>
              <span className={["mt-0.5 block text-[11px]", activeTab === id ? "text-slate-300" : "text-slate-400"].join(" ")}>
                {description}
              </span>
            </button>
          ))}
        </div>
      </div>
  );
}
