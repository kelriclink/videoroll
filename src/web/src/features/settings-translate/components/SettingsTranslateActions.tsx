import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button } from "../../../components/ui";
export function SettingsTranslateActions({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
    busy,
    saveSettings,
    clearOpenAiKey,
    clearEmbeddingKey,
  } = controller;
  return (
<div className="flex flex-wrap items-center gap-2">
        <Button tone="primary" disabled={busy} onClick={saveSettings}>{busy ? "保存中..." : "保存配置"}</Button>
        <Button
          tone="danger"
          disabled={busy || !settings?.openai_api_key_set}
          onClick={clearOpenAiKey}
        >
          清除 Key
        </Button>
        <Button
          tone="danger"
          disabled={busy || !settings?.rag_embedding_api_key_set}
          onClick={clearEmbeddingKey}
        >
          清除 Embedding Key
        </Button>
      </div>
  );
}
