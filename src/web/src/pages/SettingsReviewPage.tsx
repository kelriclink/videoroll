import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type ReviewSettings = {
  enabled: boolean;
  blocked_words: string[];
  ai_rules: string;
};

export default function SettingsReviewPage() {
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

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-lg font-semibold">Settings · Review</div>
            <div className="mt-1 text-sm text-slate-600">投稿前先根据视频标题、AI 总结和字幕内容执行 AI 审核。</div>
          </div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
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
                AI 审核已启用，但 OpenAI API Key 还没有配置。请先到 <Link className="underline" to="/settings/translate">Settings · Translate</Link> 保存，
                否则投稿前会被拦截。
              </div>
            ) : null}

            <div className="mt-4 flex items-center gap-2">
              <button
                disabled={busy}
                className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    const blocked_words = blockedWordsText
                      .split(/\r?\n/)
                      .map((item) => item.trim())
                      .filter(Boolean);
                    const saved = await fetchJson<ReviewSettings>(`${ORCHESTRATOR_URL}/settings/review`, {
                      method: "PUT",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        enabled,
                        blocked_words,
                        ai_rules: aiRules,
                      }),
                    });
                    setSettings(saved);
                    setBlockedWordsText((saved.blocked_words ?? []).join("\n"));
                    setAiRules(saved.ai_rules || "");
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
