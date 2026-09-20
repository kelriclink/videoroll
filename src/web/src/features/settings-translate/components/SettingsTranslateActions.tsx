import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, SettingsSaveBar } from "../../../components/ui";
export function SettingsTranslateActions({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    busy,
    isDirty,
    activeTab,
    saveSettings,
    discardChanges,
    clearOpenAiKey,
    clearEmbeddingKey,
  } = controller;
  if (activeTab === "test") return null;
  return (
    <SettingsSaveBar
      dirty={isDirty}
      busy={busy}
      onSave={saveSettings}
      onDiscard={discardChanges}
      extraActions={
        <>
          {activeTab === "translation" ? (
            <Button tone="danger" size="xs" disabled={busy || !settings?.openai_api_key_set} onClick={clearOpenAiKey}>
              清除翻译 Key
            </Button>
          ) : null}
          {activeTab === "embedding" ? (
            <Button tone="danger" size="xs" disabled={busy || !settings?.rag_embedding_api_key_set} onClick={clearEmbeddingKey}>
              清除 Embedding Key
            </Button>
          ) : null}
        </>
      }
    />
  );
}
