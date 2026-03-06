import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type YouTubeSettings = {
  proxy: string;
  cookies_set?: boolean;
  cookies_enabled?: boolean;
  cookies_updated_at?: string | null;
  cookies_count?: number;
  cookies_domains_count?: number;
  cookies_has_auth?: boolean;
  cookies_has_bot_check_bypass?: boolean;
  cookies_has_visitor_info?: boolean;
  cookie_file_configured?: boolean;
  cookie_file_exists?: boolean;
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
  const [cookiesTxt, setCookiesTxt] = useState("");
  const [cookiesBusy, setCookiesBusy] = useState(false);
  const [cookiesEnabled, setCookiesEnabled] = useState(false);
  const [testUrl, setTestUrl] = useState("https://www.youtube.com/robots.txt");
  const [testBusy, setTestBusy] = useState(false);
  const [testResult, setTestResult] = useState<YouTubeProxyTestResponse | null>(null);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<YouTubeSettings>(`${ORCHESTRATOR_URL}/settings/youtube`);
      setSettings(s);
      setProxy((s.proxy ?? "").toString());
      setCookiesEnabled(Boolean(s.cookies_enabled));
      setCookiesTxt("");
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
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">cookies</div>
              <div className="mt-1 text-sm">
                {settings.cookies_set ? "已设置" : "未设置"}
                {settings.cookies_set ? ` · ${settings.cookies_enabled ? "已启用" : "未启用"}` : ""}
              </div>
              {settings.cookies_set ? (
                <div className="mt-2 text-xs text-slate-600 space-y-1">
                  <div>
                    条目：{typeof settings.cookies_count === "number" ? settings.cookies_count : "-"} · 域名：{typeof settings.cookies_domains_count === "number" ? settings.cookies_domains_count : "-"}
                  </div>
                  <div>
                    登录态：{settings.cookies_has_auth ? "看起来有" : "看起来没有"} · 反爬豁免：{settings.cookies_has_bot_check_bypass ? "有" : "没有"}
                  </div>
                  {settings.cookies_updated_at ? <div>更新时间：{settings.cookies_updated_at}</div> : null}
                  {settings.cookies_enabled && settings.cookies_set && settings.cookies_has_auth === false ? (
                    <div className="text-amber-700">
                      提示：当前 cookies 可能不包含登录态（缺少 SID/SAPISID 等），遇到“Sign in to confirm you’re not a bot”时通常无效。
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">cookies 文件（YOUTUBE_COOKIE_FILE）</div>
              <div className="mt-1 text-sm">
                {settings.cookie_file_configured
                  ? `已配置 · ${settings.cookie_file_exists ? "文件存在" : "文件不存在"}`
                  : "未配置"}
              </div>
              {!settings.cookie_file_configured && settings.cookies_set ? (
                <div className="mt-2 text-xs text-slate-600">
                  未配置 <span className="font-mono">YOUTUBE_COOKIE_FILE</span> 也没关系：已保存的 cookies 会在下载/元信息提取时写入临时文件供 yt-dlp 使用。
                </div>
              ) : null}
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

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Cookies 设置</div>
        <div className="mt-2 text-xs text-slate-500">
          将浏览器导出的 <span className="font-mono">cookies.txt</span>（Netscape 格式）粘贴到下面。保存后用于 yt-dlp 下载/元信息提取。
        </div>
        <div className="mt-2 text-xs text-slate-500 space-y-1">
          <div>建议：用浏览器无痕/新会话登录 YouTube → 导出 cookies.txt → 立刻粘贴保存 → 关闭无痕窗口（避免 cookies 被轮换导致很快失效）。</div>
          <div>如果配置了代理，请确保浏览器也使用同一个代理/IP 导出并通过验证码，否则服务端仍可能触发风控。</div>
        </div>
        <label className="mt-3 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={cookiesEnabled}
            onChange={(e) => setCookiesEnabled(e.target.checked)}
          />
          启用 cookies
        </label>
        <label className="mt-3 block">
          <div className="mb-1 text-xs text-slate-600">cookies.txt 内容（留空不修改；清空请点“清空”）</div>
          <textarea
            className="h-40 w-full rounded border px-3 py-2 font-mono text-xs"
            placeholder="# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\t...\n..."
            value={cookiesTxt}
            onChange={(e) => setCookiesTxt(e.target.value)}
          />
        </label>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={cookiesBusy || !(cookiesTxt ?? "").trim()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setCookiesBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/youtube`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ cookies_txt: cookiesTxt, cookies_enabled: cookiesEnabled }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setCookiesBusy(false);
              }
            }}
          >
            保存 Cookies
          </button>
          <button
            disabled={cookiesBusy}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              setCookiesBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/youtube`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ cookies_enabled: cookiesEnabled }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setCookiesBusy(false);
              }
            }}
          >
            保存开关
          </button>
          <button
            disabled={cookiesBusy || !settings?.cookies_set}
            className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
            onClick={async () => {
              if (!confirm("确定清空已保存的 YouTube Cookies 吗？")) return;
              setCookiesBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/youtube`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ cookies_txt: "" }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setCookiesBusy(false);
              }
            }}
          >
            清空 Cookies
          </button>
        </div>
      </div>
    </div>
  );
}
