import { useEffect, useMemo, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { Button, Section, SettingsSaveBar } from "../components/ui";
import { useUnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
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

  const isDirty = Boolean(tokenInput.trim());
  useUnsavedChangesGuard(isDirty, { message: "离开当前页面会丢失尚未保存的新远程 API Token。" });

  async function saveToken() {
    if (!tokenInput.trim()) return;
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
  }

  async function clearToken() {
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
  }

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
      <div className="px-1">
        <h2 className="text-xl font-semibold tracking-tight text-slate-950">远程 API</h2>
        <div className="mt-1 text-sm text-slate-600">配置远程管理 token。外部请求使用 Bearer 鉴权、JSON 请求体和幂等键后，会按 Auto Mode 创建任务并开始处理。</div>
        {error ? <div className="mt-3 whitespace-pre-wrap break-words text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">远程调用 Token</div>
              <div className="mt-1 text-sm">{settings.token_set ? "已设置" : "未设置"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">最后更新时间</div>
              <div className="mt-1 font-mono text-sm">{settings.token_updated_at || "-"}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">说明：当前远程入口仅支持 YouTube 视频链接，行为等价于网页里的 “YouTube 自动模式”。每个逻辑请求必须携带稳定且唯一的幂等键；网络重试时复用同一个键。</div>
      </div>

      <Section>
        <div className="text-sm font-semibold">更新 Token</div>
        <div className="mt-1 text-xs text-slate-500">输入新 Token 后通过页面底部保存栏提交；保存后不会回显明文。</div>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">Token（最少 8 个字符）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder={settings?.token_set ? "已设置（输入新 Token 会覆盖旧值）" : "输入新的远程调用 Token"}
            />
          </label>
        </div>
        <div className="mt-3 text-xs text-slate-500">提示：保存后不会回显明文 Token。请自行保管；需要更换时直接输入新 Token 覆盖即可。</div>
      </Section>

      <div className="vr-section">
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

      <div className="vr-section">
        <div className="text-sm font-semibold">Chrome / Edge 右键扩展</div>
        <div className="mt-2 text-sm text-slate-600">
          仓库中的 <span className="font-mono">extensions/videoroll-youtube-submit</span> 可以在 YouTube 视频页或缩略图上右键，将视频直接提交到自动模式。
          扩展设置中的 API 地址填写下面的完整地址，Token 使用本页保存的同一个 Token。
        </div>
        <div className="mt-3 rounded border bg-slate-50 p-3 font-mono text-xs text-slate-800 break-all">{remoteEndpoint}</div>
        <div className="mt-2 text-xs text-slate-500">
          执行 <span className="font-mono">./scripts/build_browser_extension.sh</span> 可生成 <span className="font-mono">dist/videoroll-youtube-submit.zip</span>；解压后在浏览器扩展管理页选择“加载已解压的扩展程序”。
        </div>
      </div>
      <SettingsSaveBar
        dirty={isDirty}
        busy={busy}
        onSave={saveToken}
        onDiscard={() => {
          setTokenInput("");
          setError(null);
        }}
        dirtyLabel="有尚未保存的新远程 API Token"
        extraActions={
          <Button tone="danger" disabled={busy || !settings?.token_set} onClick={() => void clearToken()}>
            清空已保存 Token
          </Button>
        }
      />
    </div>
  );
}
