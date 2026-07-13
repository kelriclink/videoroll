import { useEffect, useMemo, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL, orchestratorUrl } from "../lib/urls";

type RemoteApiSettings = {
  token_set: boolean;
  token_updated_at?: string | null;
  endpoint_path: string;
};

const DEFAULT_SETTINGS: RemoteApiSettings = {
  token_set: false,
  token_updated_at: null,
  endpoint_path: "/remote/auto/youtube",
};

function orchestratorBaseUrl(): string {
  const raw = ORCHESTRATOR_URL.replace(/\/+$/, "");
  if (raw.startsWith("http://") || raw.startsWith("https://")) return raw;
  if (typeof window === "undefined") return raw;
  return new URL(raw || "/api", window.location.origin).toString().replace(/\/+$/, "");
}

export default function SettingsApiPage() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<RemoteApiSettings | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setError(null);
    try {
      const s = await fetchJson<RemoteApiSettings>(orchestratorUrl("/settings/api"));
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
  const sampleCurl = useMemo(
    () =>
      [
        `curl -X POST "${remoteEndpoint}" \\`,
        "  -H \"Authorization: Bearer YOUR_TOKEN\" \\",
        "  -H \"Idempotency-Key: YOUR_STABLE_REQUEST_ID\" \\",
        `  -H "Content-Type: application/json" \\`,
        "  --data '{\"url\":\"https://www.youtube.com/watch?v=dQw4w9WgXcQ\",\"license\":\"authorized\",\"auto_publish\":true}'",
      ].join("\n"),
    [remoteEndpoint],
  );

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Settings · API</div>
        <div className="mt-1 text-sm text-slate-600">配置远程管理 token。外部请求使用 Bearer 鉴权、JSON 请求体和幂等键后，会按 Auto Mode 创建任务并开始处理。</div>
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
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">说明：当前远程入口仅支持 YouTube 视频链接，行为等价于网页里的 “YouTube 自动模式”。每个逻辑请求必须携带稳定且唯一的幂等键；网络重试时复用同一个键。</div>
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
                await fetchJson(orchestratorUrl("/settings/api"), {
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
              const ok = await confirm({
                title: "清空远程管理 token",
                message: "清空后外部将无法再调用该接口。",
                confirmLabel: "清空",
                tone: "danger",
              });
              if (!ok) return;
              setBusy(true);
              setError(null);
              try {
                await fetchJson(orchestratorUrl("/settings/api"), {
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
          仅支持 <span className="font-mono">POST</span> JSON 请求。必填请求头：<span className="font-mono">Authorization: Bearer</span> 和 <span className="font-mono">Idempotency-Key</span>。
          请求体必填 <span className="font-mono">url</span>；可选 <span className="font-mono">license</span>、<span className="font-mono">proof_url</span>、<span className="font-mono">auto_publish</span>。
        </div>
        <div className="mt-3 rounded border bg-slate-50 p-3">
          <div className="text-xs text-slate-500">curl</div>
          <pre className="mt-2 whitespace-pre-wrap break-all font-mono text-xs text-slate-800">{sampleCurl}</pre>
        </div>
      </div>
    </div>
  );
}
