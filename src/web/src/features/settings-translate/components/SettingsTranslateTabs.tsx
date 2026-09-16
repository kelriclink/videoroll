import type { SettingsTranslateController } from "../useSettingsTranslateController";
import type { TranslateSettingsTab } from "../form";

export function SettingsTranslateTabs({ controller }: { controller: SettingsTranslateController }) {
  const {
    activeTab,
    setActiveTab
  } = controller;
  return (
<div className="vr-section">
        <div className="flex flex-wrap gap-2">
          {[
            ["translation", "翻译模型"],
            ["rag", "RAG Agent"],
            ["embedding", "本地 Embedding"],
            ["test", "测试维护"],
          ].map(([id, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => setActiveTab(id as TranslateSettingsTab)}
              className={[
                "rounded-md border px-3 py-2 text-sm",
                activeTab === id ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 text-slate-700 hover:bg-slate-50",
              ].join(" ")}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
  );
}
