import { SettingsTranslateActions } from "./components/SettingsTranslateActions";
import { SettingsTranslateHeader } from "./components/SettingsTranslateHeader";
import { SettingsTranslateSummary } from "./components/SettingsTranslateSummary";
import { SettingsTranslateTabs } from "./components/SettingsTranslateTabs";
import { EmbeddingTab } from "./components/EmbeddingTab";
import { RagTab } from "./components/RagTab";
import { TranslationTestTab } from "./components/TranslationTestTab";
import { TranslationTab } from "./components/TranslationTab";
import type { SettingsTranslateController } from "./useSettingsTranslateController";

export function SettingsTranslateView({ controller }: { controller: SettingsTranslateController }) {
  return (
    <div className="space-y-4">
      <SettingsTranslateHeader controller={controller} />
      <SettingsTranslateSummary controller={controller} />
      <SettingsTranslateTabs controller={controller} />
      {controller.activeTab === "translation" ? <TranslationTab controller={controller} /> : null}
      {controller.activeTab === "rag" ? <RagTab controller={controller} /> : null}
      {controller.activeTab === "embedding" ? <EmbeddingTab controller={controller} /> : null}
      <SettingsTranslateActions controller={controller} />
      {controller.activeTab === "test" ? <TranslationTestTab controller={controller} /> : null}
    </div>
  );
}
