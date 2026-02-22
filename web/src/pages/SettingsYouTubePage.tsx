import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type YouTubeSettings = {
  proxy: string;
};

type YouTubeProxyTestResponse = {
  ok: boolean;
  url: string;
  used_proxy?: string | null;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string | null;
};

export default function SettingsYouTubePage() {
  const [settings, setSettings] = useState<YouTubeSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [proxy, setProxy] = useState("");
  const [testUrl, setTestUrl] = useState("https://www.youtube.com/robots.txt");
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<YouTubeProxyTestResponse | null>(null);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<YouTubeSettings>(`${ORCHESTRATOR_URL}/settings/youtube`);
      setSettings(s);
      setProxy((s.proxy ?? "").toString());
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
        <div className="text-lg font-semibold">Settings · YouTube</div>
        <div className="mt-1 text-sm text-slate-600">只影响 YouTube 的下载/元信息/封面/订阅扫描等请求。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">proxy</div>
              <div className="mt-1 font-mono text-sm break-all">{settings.proxy || "-"}</div>
            </div>
          </div>
        )}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">代理设置</div>
        <div className="mt-2 text-xs text-slate-500">
          例如：<span className="font-mono">http://127.0.0.1:7890</span> / <span className="font-mono">socks5://127.0.0.1:1080</span>（留空=不使用代理）
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">proxy</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              placeholder="http://host:port"
              value={proxy}
              onChange={(e) => setProxy(e.target.value)}
            />
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">测试链接</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={testUrl} onChange={(e) => setTestUrl(e.target.value)} />
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
                await fetchJson(`${ORCHESTRATOR_URL}/settings/youtube`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ proxy }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            保存
          </button>

          <button
            disabled={testBusy}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              setTestBusy(true);
              setTestResult(null);
              setError(null);
              try {
                const res = await fetchJson<YouTubeProxyTestResponse>(`${ORCHESTRATOR_URL}/settings/youtube/test`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ proxy, url: testUrl }),
                });
                setTestResult(res);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setTestBusy(false);
              }
            }}
          >
            测试链接
          </button>
        </div>

        {testResult ? (
          <div className="mt-3 rounded border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <div className={testResult.ok ? "text-emerald-700" : "text-rose-700"}>{testResult.ok ? "OK" : "FAILED"}</div>
              <div className="text-slate-600">status={testResult.status_code ?? "-"}</div>
              <div className="text-slate-600">elapsed={testResult.elapsed_ms}ms</div>
            </div>
            <div className="mt-2 text-xs text-slate-600 break-all">url: {testResult.url}</div>
            <div className="mt-1 text-xs text-slate-600 break-all">proxy: {testResult.used_proxy ?? "(none)"}</div>
            {testResult.error ? <div className="mt-2 text-xs text-rose-700 break-all">{testResult.error}</div> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

