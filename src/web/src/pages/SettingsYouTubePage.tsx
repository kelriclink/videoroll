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
  home_scan_enabled?: boolean;
  home_scan_interval_minutes?: number;
  home_scan_limit?: number;
  home_scan_long_videos_only?: boolean;
  home_scan_min_duration_seconds?: number;
  home_scan_running?: boolean;
  home_scan_last_started_at?: string | null;
  home_scan_last_finished_at?: string | null;
  home_scan_last_discovered_count?: number;
  home_scan_last_started_count?: number;
  home_scan_last_skipped_duplicates?: number;
  home_scan_last_failed_count?: number;
  home_scan_last_candidate_count?: number;
  home_scan_last_explicit_shorts_count?: number;
  home_scan_last_known_duration_count?: number;
  home_scan_last_unknown_duration_count?: number;
  home_scan_last_below_min_duration_count?: number;
  home_scan_last_kept_unknown_duration_count?: number;
  home_scan_last_eligible_count?: number;
  home_scan_last_log_lines?: string[];
  home_scan_last_error?: string | null;
  home_scan_last_sample_urls?: string[];
};

type YouTubeProxyTestResponse = {
  ok: boolean;
  url: string;
  used_proxy?: string | null;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string | null;
};

type YouTubeHomeScanRunResponse = {
  discovered_count: number;
  created_task_ids: string[];
  skipped_duplicates: number;
  failed_count?: number;
  candidate_count?: number;
  explicit_shorts_count?: number;
  known_duration_count?: number;
  unknown_duration_count?: number;
  below_min_duration_count?: number;
  kept_unknown_duration_count?: number;
  eligible_count?: number;
  min_duration_seconds?: number;
  log_lines?: string[];
  started_pipeline_job_ids?: string[];
  sample_urls?: string[];
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
  const [homeScanEnabled, setHomeScanEnabled] = useState(false);
  const [homeScanIntervalMinutes, setHomeScanIntervalMinutes] = useState(60);
  const [homeScanLimit, setHomeScanLimit] = useState(10);
  const [homeScanLongVideosOnly, setHomeScanLongVideosOnly] = useState(false);
  const [homeScanMinDurationSeconds, setHomeScanMinDurationSeconds] = useState(180);
  const [homeScanBusy, setHomeScanBusy] = useState(false);
  const [homeScanRunBusy, setHomeScanRunBusy] = useState(false);
  const [homeScanResult, setHomeScanResult] = useState<YouTubeHomeScanRunResponse | null>(null);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<YouTubeSettings>(`${ORCHESTRATOR_URL}/settings/youtube`);
      setSettings(s);
      setProxy((s.proxy ?? "").toString());
      setCookiesEnabled(Boolean(s.cookies_enabled));
      setHomeScanEnabled(Boolean(s.home_scan_enabled));
      setHomeScanIntervalMinutes(Math.max(1, Number(s.home_scan_interval_minutes ?? 60) || 60));
      setHomeScanLimit(Math.max(1, Number(s.home_scan_limit ?? 10) || 10));
      setHomeScanLongVideosOnly(Boolean(s.home_scan_long_videos_only));
      setHomeScanMinDurationSeconds(Math.max(0, Number(s.home_scan_min_duration_seconds ?? 180) || 0));
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
        <div className="mt-1 text-sm text-slate-600">影响 YouTube 的下载、元信息、封面，以及首页推荐定时扫描请求。</div>
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
        <div className="text-sm font-semibold">首页推荐定时扫描</div>
        <div className="mt-2 text-xs text-slate-500">
          使用已保存的 YouTube 登录 cookies 扫描首页推荐视频；发现新链接后，直接进入现有 YouTube 自动模式，后续字幕生成和投稿参数继续按 <span className="font-mono">Settings · Auto Mode</span> 执行。
        </div>
        <div className="mt-2 text-xs text-slate-500">
          开启“仅抓长视频”后，系统会先额外抓一批候选，再过滤显式 Shorts；如果解析到了时长，还会按你设置的最短时长继续筛选。YouTube 首页里有些正常视频本身不带时长字段，这类候选会在日志里单独统计。
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <div className="block md:col-span-3">
            <div className="mb-1 text-xs text-slate-600">开关</div>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={homeScanEnabled} onChange={(e) => setHomeScanEnabled(e.target.checked)} />
              启用定时扫描
            </label>
          </div>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">扫描间隔（分钟）</div>
            <input
              type="number"
              min={1}
              max={1440}
              className="w-full rounded border px-3 py-2 text-sm"
              value={homeScanIntervalMinutes}
              onChange={(e) => setHomeScanIntervalMinutes(Math.max(1, parseInt(e.target.value || "60", 10) || 60))}
            />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">每次抓取数量</div>
            <input
              type="number"
              min={1}
              max={100}
              className="w-full rounded border px-3 py-2 text-sm"
              value={homeScanLimit}
              onChange={(e) => setHomeScanLimit(Math.max(1, parseInt(e.target.value || "10", 10) || 10))}
            />
          </label>
          <div className="rounded border p-3 text-xs text-slate-600">
            <div className="font-medium text-slate-700">过滤</div>
            <label className="mt-2 flex items-center gap-2 text-sm">
              <input type="checkbox" checked={homeScanLongVideosOnly} onChange={(e) => setHomeScanLongVideosOnly(e.target.checked)} />
              仅抓长视频
            </label>
            <label className="mt-3 block">
              <div className="mb-1 text-xs text-slate-600">最短时长（秒）</div>
              <input
                type="number"
                min={0}
                max={86400}
                className="w-full rounded border px-3 py-2 text-sm"
                value={homeScanMinDurationSeconds}
                onChange={(e) => setHomeScanMinDurationSeconds(Math.max(0, parseInt(e.target.value || "0", 10) || 0))}
              />
            </label>
            <div className="mt-2">
              <span className="font-mono">0</span> 表示只排除 Shorts，不再按时长阈值排除；大于 <span className="font-mono">0</span> 时，只对已解析出时长的候选应用阈值。
            </div>
          </div>
          <div className="rounded border p-3 text-xs text-slate-600">
            <div>状态：{settings?.home_scan_running ? "运行中" : homeScanEnabled ? "已启用" : "未启用"}</div>
            <div className="mt-1">长视频过滤：{settings?.home_scan_long_videos_only ? "开启" : "关闭"}</div>
            <div className="mt-1">最短时长：{settings?.home_scan_min_duration_seconds ?? 0} 秒</div>
            <div className="mt-1">最近开始：{settings?.home_scan_last_started_at ?? "-"}</div>
            <div className="mt-1">最近完成：{settings?.home_scan_last_finished_at ?? "-"}</div>
            <div className="mt-1">
              最近结果：发现 {settings?.home_scan_last_discovered_count ?? 0} / 启动 {(settings?.home_scan_last_started_count ?? 0)} / 跳过 {(settings?.home_scan_last_skipped_duplicates ?? 0)} / 失败 {(settings?.home_scan_last_failed_count ?? 0)}
            </div>
            <div className="mt-1">
              候选统计：原始 {settings?.home_scan_last_candidate_count ?? 0} / 显式 Shorts {settings?.home_scan_last_explicit_shorts_count ?? 0} / 已知时长 {settings?.home_scan_last_known_duration_count ?? 0} / 未知时长 {settings?.home_scan_last_unknown_duration_count ?? 0}
            </div>
            <div className="mt-1">
              过滤结果：低于阈值 {settings?.home_scan_last_below_min_duration_count ?? 0} / 保留未知时长 {settings?.home_scan_last_kept_unknown_duration_count ?? 0} / 最终可用 {settings?.home_scan_last_eligible_count ?? 0}
            </div>
          </div>
        </div>
        {settings?.home_scan_last_error ? <div className="mt-3 text-xs text-rose-700 break-all">{settings.home_scan_last_error}</div> : null}
        {settings?.home_scan_last_sample_urls?.length ? (
          <div className="mt-3 rounded border p-3">
            <div className="text-xs font-medium text-slate-700">最近扫描样本</div>
            <div className="mt-2 space-y-1 text-xs text-slate-600">
              {settings.home_scan_last_sample_urls.map((url) => (
                <div key={url} className="break-all">
                  {url}
                </div>
              ))}
            </div>
          </div>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={homeScanBusy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setHomeScanBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/youtube`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    home_scan_enabled: homeScanEnabled,
                    home_scan_interval_minutes: homeScanIntervalMinutes,
                    home_scan_limit: homeScanLimit,
                    home_scan_long_videos_only: homeScanLongVideosOnly,
                    home_scan_min_duration_seconds: homeScanMinDurationSeconds,
                  }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setHomeScanBusy(false);
              }
            }}
          >
            保存扫描设置
          </button>
          <button
            disabled={homeScanRunBusy}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              setHomeScanRunBusy(true);
              setHomeScanResult(null);
              setError(null);
              try {
                const res = await fetchJson<YouTubeHomeScanRunResponse>(`${ORCHESTRATOR_URL}/settings/youtube/home_scan/run`, {
                  method: "POST",
                });
                setHomeScanResult(res);
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setHomeScanRunBusy(false);
              }
            }}
          >
            立即扫描一次
          </button>
        </div>
        {homeScanResult ? (
          <div className="mt-3 rounded border p-3 text-sm">
            <div>本次发现：{homeScanResult.discovered_count}</div>
            <div className="mt-1">
              新建任务：{homeScanResult.created_task_ids.length} · 启动流水线：{(homeScanResult.started_pipeline_job_ids ?? []).length} · 跳过重复：{homeScanResult.skipped_duplicates} · 失败：{homeScanResult.failed_count ?? 0}
            </div>
            <div className="mt-1 text-xs text-slate-600">
              候选统计：原始 {homeScanResult.candidate_count ?? 0} / 显式 Shorts {homeScanResult.explicit_shorts_count ?? 0} / 已知时长 {homeScanResult.known_duration_count ?? 0} / 未知时长 {homeScanResult.unknown_duration_count ?? 0}
            </div>
            <div className="mt-1 text-xs text-slate-600">
              过滤结果：低于阈值 {homeScanResult.below_min_duration_count ?? 0} / 保留未知时长 {homeScanResult.kept_unknown_duration_count ?? 0} / 最终可用 {homeScanResult.eligible_count ?? 0} / 最短时长 {homeScanResult.min_duration_seconds ?? 0} 秒
            </div>
            {homeScanResult.log_lines?.length ? (
              <div className="mt-3 rounded border bg-slate-50 p-3">
                <div className="text-xs font-medium text-slate-700">本次扫描日志</div>
                <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] text-slate-700">{homeScanResult.log_lines.join("\n")}</pre>
              </div>
            ) : null}
            {homeScanResult.sample_urls?.length ? (
              <div className="mt-2 space-y-1 text-xs text-slate-600">
                {homeScanResult.sample_urls.map((url) => (
                  <div key={url} className="break-all">
                    {url}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {settings?.home_scan_last_log_lines?.length ? (
          <div className="mt-3 rounded border bg-slate-50 p-3">
            <div className="text-xs font-medium text-slate-700">最近一次扫描日志</div>
            <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] text-slate-700">{settings.home_scan_last_log_lines.join("\n")}</pre>
          </div>
        ) : null}
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
