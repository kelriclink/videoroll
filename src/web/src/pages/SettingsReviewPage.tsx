import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { Button, Section, SettingsSaveBar } from "../components/ui";
import { useUnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type ReviewSettings = {
  enabled: boolean;
  blocked_words: string[];
  ai_rules: string;
};

export default function SettingsReviewPage() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<ReviewSettings | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [blockedWordsText, setBlockedWordsText] = useState("");
  const [aiRules, setAiRules] = useState("");
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setError(null);
    try {
      const [cfg, translateCfg] = await Promise.all([
        fetchJson<ReviewSettings>(`${ORCHESTRATOR_URL}/settings/review`),
        fetchJson<{ openai_api_key_set: boolean }>(`${ORCHESTRATOR_URL}/subtitle/translate/settings`).catch(() => null),
      ]);
      setSettings(cfg);
      setEnabled(Boolean(cfg.enabled));
      setBlockedWordsText((Array.isArray(cfg.blocked_words) ? cfg.blocked_words : []).join("\n"));
      setAiRules(cfg.ai_rules || "");
      if (translateCfg) setOpenaiKeySet(Boolean(translateCfg.openai_api_key_set));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const normalizedBlockedWords = blockedWordsText
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  const isDirty =
    Boolean(settings) &&
    (
      enabled !== Boolean(settings?.enabled) ||
      JSON.stringify(normalizedBlockedWords) !== JSON.stringify(settings?.blocked_words ?? []) ||
      aiRules !== (settings?.ai_rules || "")
    );
  useUnsavedChangesGuard(isDirty, { message: "离开当前页面会丢失尚未保存的审核规则。" });

  async function reloadFromServer() {
    if (isDirty) {
      const ok = await confirm({
        title: "刷新并放弃未保存修改",
        message: "刷新会重新载入审核配置，并覆盖当前尚未保存的规则。",
        confirmLabel: "刷新并放弃",
        cancelLabel: "继续编辑",
        tone: "warning",
      });
      if (!ok) return;
    }
    await refresh();
  }

  async function saveReviewSettings() {
    setBusy(true);
    setError(null);
    try {
      const saved = await fetchJson<ReviewSettings>(`${ORCHESTRATOR_URL}/settings/review`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled,
          blocked_words: normalizedBlockedWords,
          ai_rules: aiRules,
        }),
      });
      setSettings(saved);
      setEnabled(Boolean(saved.enabled));
      setBlockedWordsText((saved.blocked_words ?? []).join("\n"));
      setAiRules(saved.ai_rules || "");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="px-1">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold tracking-tight text-slate-950">投稿前审核</h2>
            <div className="mt-1 text-sm text-slate-600">投稿前先根据视频标题、AI 总结和字幕内容执行 AI 审核。</div>
          </div>
          <Button onClick={() => void reloadFromServer()}>刷新</Button>
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <Section>
        {!settings ? <div className="text-sm text-slate-500">加载中…</div> : null}
        {settings ? (
          <>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              启用投稿前 AI 审核
            </label>

            <div className="mt-4 grid gap-4">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">违禁词（每行一个）</div>
                <textarea
                  className="h-40 w-full rounded border p-3 font-mono text-xs"
                  value={blockedWordsText}
                  onChange={(e) => setBlockedWordsText(e.target.value)}
                  placeholder={`政治敏感词\n诈骗引流\n成人内容`}
                />
                <div className="mt-2 text-xs text-slate-500">命中这些词会直接判定不通过，不再继续投稿。</div>
              </label>

              <label className="block">
                <div className="mb-1 text-xs text-slate-600">补充审核规则（告诉 AI 什么视频不能过）</div>
                <textarea
                  className="h-48 w-full rounded border p-3 text-sm"
                  value={aiRules}
                  onChange={(e) => setAiRules(e.target.value)}
                  placeholder="例如：包含血腥处刑、吸毒教程、灰产引流、未成年人擦边、赌博/私彩导流的视频，一律不通过。"
                />
                <div className="mt-2 text-xs text-slate-500">这里写的是额外业务规则，AI 会和标题/总结/字幕一起综合判断。</div>
              </label>
            </div>

            {enabled && openaiKeySet === false ? (
              <div className="mt-4 rounded border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
                AI 审核已启用，但 OpenAI API Key 还没有配置。请先到 <Link className="underline" to="/settings/translate">翻译 / RAG 设置</Link> 保存，
                否则投稿前会被拦截。
              </div>
            ) : null}

          </>
        ) : null}
      </Section>
      <SettingsSaveBar
        dirty={isDirty}
        busy={busy}
        onSave={saveReviewSettings}
        onDiscard={() => {
          if (!settings) return;
          setEnabled(Boolean(settings.enabled));
          setBlockedWordsText((settings.blocked_words ?? []).join("\n"));
          setAiRules(settings.ai_rules || "");
          setError(null);
        }}
        dirtyLabel="有未保存的审核规则"
      />
    </div>
  );
}
