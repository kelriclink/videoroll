import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type TranslateSettings = {
  default_provider: string;
  default_target_lang: string;
  default_style: string;
  default_batch_size: number;
  default_enable_summary: boolean;

  openai_api_key_set: boolean;
  openai_base_url: string;
  openai_model: string;
  openai_temperature: number;
  openai_timeout_seconds: number;
};

export default function SettingsTranslatePage() {
  const [settings, setSettings] = useState<TranslateSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [defaultProvider, setDefaultProvider] = useState("openai");
  const [defaultTargetLang, setDefaultTargetLang] = useState("zh");
  const [defaultStyle, setDefaultStyle] = useState("口语自然");
  const [defaultBatchSize, setDefaultBatchSize] = useState(50);
  const [defaultEnableSummary, setDefaultEnableSummary] = useState(true);

  const [openaiBaseUrl, setOpenaiBaseUrl] = useState("https://api.openai.com/v1");
  const [openaiModel, setOpenaiModel] = useState("gpt-4o-mini");
  const [openaiTemperature, setOpenaiTemperature] = useState(0.2);
  const [openaiTimeoutSeconds, setOpenaiTimeoutSeconds] = useState(60);
  const [openaiApiKey, setOpenaiApiKey] = useState("");

  const [testText, setTestText] = useState("Hello world. This is a translation test.");
  const [testTargetLang, setTestTargetLang] = useState("zh");
  const [testStyle, setTestStyle] = useState("口语自然");
  const [testResult, setTestResult] = useState<string | null>(null);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<TranslateSettings>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`);
      setSettings(s);
      setDefaultProvider(s.default_provider);
      setDefaultTargetLang(s.default_target_lang);
      setDefaultStyle(s.default_style);
      setDefaultBatchSize(s.default_batch_size);
      setDefaultEnableSummary(s.default_enable_summary);
      setOpenaiBaseUrl(s.openai_base_url);
      setOpenaiModel(s.openai_model);
      setOpenaiTemperature(s.openai_temperature);
      setOpenaiTimeoutSeconds(s.openai_timeout_seconds);
      setTestTargetLang(s.default_target_lang || "zh");
      setTestStyle(s.default_style || "口语自然");
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
        <div className="text-lg font-semibold">Settings · Translate</div>
        <div className="mt-1 text-sm text-slate-600">配置字幕翻译（可选接入 OpenAI 标准接口）。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置（保存于后端配置/数据库）</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>

        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">default_provider</div>
              <div className="mt-1 font-mono text-sm">{settings.default_provider}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">default_target_lang</div>
              <div className="mt-1 font-mono text-sm">{settings.default_target_lang}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">default_style</div>
              <div className="mt-1 font-mono text-sm">{settings.default_style}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">default_batch_size</div>
              <div className="mt-1 font-mono text-sm">{settings.default_batch_size}</div>
            </div>
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">default_enable_summary</div>
              <div className="mt-1 text-sm">{settings.default_enable_summary ? "true" : "false"}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">提示：默认值用于前端初始选择；任务里仍可按需覆盖。</div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">保存配置</div>
          <div className="text-xs text-slate-500">
            OpenAI API Key：{settings?.openai_api_key_set ? <span className="text-emerald-700">已设置</span> : <span className="text-rose-700">未设置</span>}
          </div>
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_provider</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultProvider} onChange={(e) => setDefaultProvider(e.target.value)}>
              <option value="openai">openai</option>
              <option value="mock">mock</option>
              <option value="noop">noop</option>
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_target_lang</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={defaultTargetLang} onChange={(e) => setDefaultTargetLang(e.target.value)} />
          </label>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">default_style</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultStyle} onChange={(e) => setDefaultStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_batch_size</div>
            <input
              type="number"
              min={1}
              className="w-full rounded border px-3 py-2 text-sm"
              value={defaultBatchSize}
              onChange={(e) => setDefaultBatchSize(parseInt(e.target.value || "1", 10))}
            />
          </label>

          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={defaultEnableSummary} onChange={(e) => setDefaultEnableSummary(e.target.checked)} />
            default_enable_summary
          </label>

          <div className="md:col-span-2 pt-2 text-xs font-semibold text-slate-700">OpenAI（标准接口）</div>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_api_key（仅保存，不回显）</div>
            <input
              type="password"
              className="w-full rounded border px-3 py-2 text-sm"
              placeholder={settings?.openai_api_key_set ? "已设置（留空则不修改）" : "sk-..."}
              value={openaiApiKey}
              onChange={(e) => setOpenaiApiKey(e.target.value)}
            />
          </label>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_base_url</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiBaseUrl} onChange={(e) => setOpenaiBaseUrl(e.target.value)} />
            <div className="mt-1 text-xs text-slate-500">
              提示：多数 OpenAI 兼容接口需要 base_url 以 <span className="font-mono">/v1</span> 结尾（例如{" "}
              <span className="font-mono">https://api.openai.com/v1</span>）。如果只填域名根（例如{" "}
              <span className="font-mono">https://ai.example.com</span>），后端会自动补全为{" "}
              <span className="font-mono">https://ai.example.com/v1</span>。
            </div>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_model</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiModel} onChange={(e) => setOpenaiModel(e.target.value)} />
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_temperature</div>
            <input
              type="number"
              step="0.1"
              min={0}
              max={2}
              className="w-full rounded border px-3 py-2 text-sm"
              value={openaiTemperature}
              onChange={(e) => setOpenaiTemperature(parseFloat(e.target.value || "0"))}
            />
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_timeout_seconds</div>
            <input
              type="number"
              min={1}
              className="w-full rounded border px-3 py-2 text-sm"
              value={openaiTimeoutSeconds}
              onChange={(e) => setOpenaiTimeoutSeconds(parseFloat(e.target.value || "1"))}
            />
          </label>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const payload: any = {
                  default_provider: defaultProvider,
                  default_target_lang: defaultTargetLang,
                  default_style: defaultStyle,
                  default_batch_size: defaultBatchSize,
                  default_enable_summary: defaultEnableSummary,
                  openai_base_url: openaiBaseUrl,
                  openai_model: openaiModel,
                  openai_temperature: openaiTemperature,
                  openai_timeout_seconds: openaiTimeoutSeconds,
                };
                if (openaiApiKey.trim()) payload.openai_api_key = openaiApiKey.trim();
                await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(payload),
                });
                setOpenaiApiKey("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "保存中…" : "保存"}
          </button>

          <button
            disabled={busy || !settings?.openai_api_key_set}
            className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
            onClick={async () => {
              if (!confirm("确定清除 OpenAI API Key 吗？")) return;
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/translate/settings`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ openai_api_key: "" }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            清除 Key
          </button>
        </div>

        <div className="mt-3 text-xs text-slate-500">
          提示：API Key 会加密存储在 DB 中，解密密钥保存在本地 `data/secrets/fernet.key`（容器内 `/secrets/fernet.key`）。
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">测试翻译</div>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">target_lang</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={testTargetLang} onChange={(e) => setTestTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">style</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={testStyle} onChange={(e) => setTestStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">text</div>
            <textarea className="h-28 w-full rounded border px-3 py-2 text-sm" value={testText} onChange={(e) => setTestText(e.target.value)} />
          </label>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              setTestResult(null);
              try {
                const resp = await fetchJson<{ translated_text: string }>(`${SUBTITLE_SERVICE_URL}/subtitle/translate/test`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ text: testText, target_lang: testTargetLang, style: testStyle }),
                });
                setTestResult(resp.translated_text);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "测试中…" : "开始测试（openai）"}
          </button>
          {testResult ? <div className="text-sm text-slate-700">OK</div> : null}
        </div>
        {testResult ? (
          <div className="mt-3 rounded border bg-slate-50 p-3">
            <div className="text-xs text-slate-500">translated_text</div>
            <div className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{testResult}</div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
