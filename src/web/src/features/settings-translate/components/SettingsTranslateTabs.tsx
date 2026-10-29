import type { SettingsTranslateController } from "../useSettingsTranslateController";
import type { TranslateSettingsTab } from "../form";

export function SettingsTranslateTabs({ controller }: { controller: SettingsTranslateController }) {
  const { activeTab, setActiveTab } = controller;
  const tabs: Array<[TranslateSettingsTab, string]> = [
    ["translation", "翻译与模型"],
    ["rag", "研究与知识"],
    ["embedding", "向量索引"],
    ["test", "连接测试"],
  ];

  return (
    <div className="border-b border-slate-200 dark:border-slate-800">
      <div className="flex gap-1 overflow-x-auto">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={activeTab === id}
            onClick={() => setActiveTab(id)}
            className={[
              "shrink-0 border-b-2 px-3 py-2 text-sm transition-colors",
              activeTab === id
                ? "border-slate-950 font-medium text-slate-950 dark:border-slate-100 dark:text-white"
                : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-900 dark:hover:text-slate-200",
            ].join(" ")}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
