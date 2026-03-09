import { useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type RemoteApiSettings = {
  token_set: boolean;
  token_updated_at?: string | null;
  endpoint_path: string;
  token_query_param: string;
  url_query_param: string;
  license_query_param: string;
  proof_url_query_param: string;
};

const DEFAULT_SETTINGS: RemoteApiSettings = {
  token_set: false,
  token_updated_at: null,
  endpoint_path: "/remote/auto/youtube",
  token_query_param: "token",
  url_query_param: "url",
  license_query_param: "license",
  proof_url_query_param: "proof_url",
};

function orchestratorBaseUrl(): string {
  const raw = ORCHESTRATOR_URL.replace(/\/+$/, "");
  if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
  if (typeof window === "undefined") return raw;
  return new URL(raw || "/api", window.location.origin).toString().replace(/\/+$/, "");
}

export default function SettingsApiPage() {
  const [settings, setSettings] = useState<RemoteApiSettings | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<RemoteApiSettings>(`${ORCHESTRATOR_URL}/settings/api`);
      setSettings(s);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const effective = settings ?? DEFAULT_SETTINGS;
  const baseUrl = useMemo(() => orchestratorBaseUrl(), []);
  const remoteEndpoint = useMemo(() => `${baseUrl}${effective.endpoint_path}`, [baseUrl, effective.endpoint_path]);
  const sampleUrl = useMemo(() => {
    const qs = new URLSearchParams();
    qs.set(effective.token_query_param, tokenInput.trim() || "YOUR_TOKEN");
    qs.set(effective.url_query_param, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    qs.set(effective.license_query_param, "authorized");
    return `${remoteEndpoint}?${qs.toString()}`;
  }, [effective.license_query_param, effective.token_query_param, effective.url_query_param, remoteEndpoint, tokenInput]);
  const sampleCurl = useMemo(
    () =>
      [
        `curl -G "${remoteEndpoint}" \\`,
        `  --data-urlencode "${effective.token_query_param}=${tokenInput.trim() || "YOUR_TOKEN"}" \\`,
        `  --data-urlencode "${effective.url_query_param}=https://www.youtube.com/watch?v=dQw4w9WgXcQ" \\`,
        `  --data-urlencode "${effective.license_query_param}=authorized"`,
      ].join("\n"),
    [effective.license_query_param, effective.token_query_param, effective.url_query_param, remoteEndpoint, tokenInput],
  );

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Settings · API</div>
        <div className="mt-1 text-sm text-slate-600">配置远程管理 token。外部请求带上 token 和 YouTube `url` 参数后，会直接按 Auto Mode 自动创建任务并开始处理。</div>
        {error ? <div className="mt-3 whitespace-pre-wrap break-words text-sm text-rose-700">{error}</div> : null}
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
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">remote token</div>
              <div className="mt-1 text-sm">{settings.token_set ? "已设置" : "未设置"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">token_updated_at</div>
              <div className="mt-1 font-mono text-sm">{settings.token_updated_at || "-"}</div>
            </div>
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">remote endpoint</div>
              <div className="mt-1 break-all font-mono text-sm">{remoteEndpoint}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">说明：当前远程入口仅支持 YouTube 视频链接，行为等价于网页里的 “YouTube 自动模式”。</div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">保存 token</div>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">token（最少 8 个字符）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder={settings?.token_set ? "已设置（输入新 token 会覆盖旧值）" : "输入新的远程调用 token"}
            />
          </label>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy || !tokenInput.trim()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/api`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ token: tokenInput.trim() }),
                });
                setTokenInput("");
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
            disabled={busy || !settings?.token_set}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              if (!window.confirm("确认清空远程管理 token？清空后外部将无法再调用该接口。")) return;
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/settings/api`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ token: "" }),
                });
                setTokenInput("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            清空 token
          </button>
        </div>

        <div className="mt-3 text-xs text-slate-500">提示：保存后不会回显明文 token。请自行保管；需要更换时直接输入新 token 覆盖即可。</div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">调用示例</div>
        <div className="mt-2 text-xs text-slate-500">
          必填参数：<span className="font-mono">{effective.token_query_param}</span>、<span className="font-mono">{effective.url_query_param}</span>
          。可选参数：<span className="font-mono">{effective.license_query_param}</span>、<span className="font-mono">{effective.proof_url_query_param}</span>。
        </div>
        <div className="mt-3 rounded border bg-slate-50 p-3">
          <div className="text-xs text-slate-500">示例 URL</div>
          <pre className="mt-2 whitespace-pre-wrap break-all font-mono text-xs text-slate-800">{sampleUrl}</pre>
        </div>
        <div className="mt-3 rounded border bg-slate-50 p-3">
          <div className="text-xs text-slate-500">curl</div>
          <pre className="mt-2 whitespace-pre-wrap break-all font-mono text-xs text-slate-800">{sampleCurl}</pre>
        </div>
      </div>
    </div>
  );
}
