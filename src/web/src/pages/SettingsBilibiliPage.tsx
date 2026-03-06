import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { BILIBILI_PUBLISHER_URL } from "../lib/urls";

type PublishSettings = {
  default_meta: any;
};

type AuthSettings = {
  cookie_set: boolean;
  sessdata_set: boolean;
  bili_jct_set: boolean;
};

type BilibiliMe = {
  mid: number;
  uname: string;
  userid?: string | null;
  sign?: string | null;
  rank?: string | null;
};

export default function SettingsBilibiliPage() {
  const [settings, setSettings] = useState<PublishSettings | null>(null);
  const [auth, setAuth] = useState<AuthSettings | null>(null);
  const [metaText, setMetaText] = useState<string>("{}");
  const [cookieText, setCookieText] = useState<string>("");
  const [me, setMe] = useState<BilibiliMe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setError(null);
    try {
      const [s, a] = await Promise.all([
        fetchJson<PublishSettings>(`${BILIBILI_PUBLISHER_URL}/bilibili/publish/settings`),
        fetchJson<AuthSettings>(`${BILIBILI_PUBLISHER_URL}/bilibili/auth/settings`),
      ]);
      setSettings(s);
      setAuth(a);
      setMe(null);
      setMetaText(JSON.stringify(s.default_meta ?? {}, null, 2));
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
        <div className="text-lg font-semibold">Settings · Bilibili</div>
        <div className="mt-1 text-sm text-slate-600">配置 Cookies 登录（用于后续真实投稿）+ 默认投稿 meta 模板。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Cookies 登录</div>
        <div className="mt-2 text-xs text-slate-500">
          说明：此处保存的是用于 B 站接口调用的 Cookie（加密存储，后端不会回显）。请从浏览器开发者工具复制整段 Cookie。
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">cookie_set</div>
            <div className="mt-1 text-sm">{auth?.cookie_set ? "true" : "false"}</div>
          </div>
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">解析</div>
            <div className="mt-1 text-sm">
              SESSDATA：{auth?.sessdata_set ? "✅" : "❌"} / bili_jct：{auth?.bili_jct_set ? "✅" : "❌"}
            </div>
          </div>
        </div>

        <div className="mt-3">
          <div className="mb-1 text-xs text-slate-600">Cookie（仅保存，不回显）</div>
          <input
            type="password"
            className="w-full rounded border px-3 py-2 font-mono text-xs"
            placeholder={auth?.cookie_set ? "已设置（留空不修改）" : "SESSDATA=...; bili_jct=...; ..."}
            value={cookieText}
            onChange={(e) => setCookieText(e.target.value)}
          />
          <div className="mt-1 text-xs text-slate-500">提示：真实投稿会需要 bili_jct（csrf），建议确保 Cookie 里包含该字段。</div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy || !cookieText.trim()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${BILIBILI_PUBLISHER_URL}/bilibili/auth/settings`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ cookie: cookieText.trim() }),
                });
                setCookieText("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "保存中…" : "保存 Cookies"}
          </button>

          <button
            disabled={busy || !auth?.cookie_set}
            className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
            onClick={async () => {
              if (!confirm("确定清除已保存的 Cookies 吗？")) return;
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${BILIBILI_PUBLISHER_URL}/bilibili/auth/settings`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ cookie: "" }),
                });
                setCookieText("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            清除 Cookies
          </button>

          <button
            disabled={busy || !(auth?.sessdata_set || auth?.cookie_set)}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const info = await fetchJson<BilibiliMe>(`${BILIBILI_PUBLISHER_URL}/bilibili/auth/me`);
                setMe(info);
              } catch (e: unknown) {
                setMe(null);
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            测试登录
          </button>
        </div>

        {me ? (
          <div className="mt-3 rounded border bg-slate-50 p-3 text-sm text-slate-700">
            <div className="font-semibold text-slate-700">已登录</div>
            <div className="mt-1 font-mono text-xs">
              mid={me.mid} uname={me.uname} {me.userid ? `userid=${me.userid}` : ""}
            </div>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">default_meta.json</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>

        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <>
            <div className="mt-3">
              <textarea
                className="h-96 w-full rounded border p-3 font-mono text-xs"
                value={metaText}
                onChange={(e) => setMetaText(e.target.value)}
              />
              <div className="mt-2 text-xs text-slate-500">
                说明：这里保存的是“默认模板”。实际投稿时仍可在任务详情页按需修改。
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                disabled={busy}
                className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    const meta = JSON.parse(metaText);
                    await fetchJson(`${BILIBILI_PUBLISHER_URL}/bilibili/publish/settings`, {
                      method: "PUT",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ default_meta: meta }),
                    });
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
                disabled={busy}
                className="rounded border border-rose-300 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                onClick={async () => {
                  if (!confirm("确定恢复内置默认模板吗？")) return;
                  setBusy(true);
                  setError(null);
                  try {
                    await fetchJson(`${BILIBILI_PUBLISHER_URL}/bilibili/publish/settings`, {
                      method: "PUT",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ default_meta: {} }),
                    });
                    await refresh();
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                恢复默认
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
