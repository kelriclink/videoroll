import { useEffect, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { fetchJson } from "../lib/http";
import { BILIBILI_PUBLISHER_URL, ORCHESTRATOR_URL } from "../lib/urls";
import {
  activeAccountsForPlatform,
  loginSessionLabel,
  PublishPlatform,
  PublishPlatformSettings,
  SocialAccount,
  SocialLoginSession,
  SocialPlatform,
  storageStateGuidance,
} from "./settingsPublishPage.helpers";

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

type PublishPlatformSettingsResponse = {
  platforms: PublishPlatformSettings;
};

const SOCIAL_PLATFORMS: Array<{ id: SocialPlatform; label: string }> = [
  { id: "douyin", label: "抖音" },
  { id: "xiaohongshu", label: "小红书" },
  { id: "kuaishou", label: "快手" },
];

export default function SettingsPublishPage() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<PublishSettings | null>(null);
  const [auth, setAuth] = useState<AuthSettings | null>(null);
  const [metaText, setMetaText] = useState<string>("{}");
  const [cookieText, setCookieText] = useState<string>("");
  const [me, setMe] = useState<BilibiliMe | null>(null);
  const [socialAccounts, setSocialAccounts] = useState<SocialAccount[]>([]);
  const [platformSettings, setPlatformSettings] = useState<PublishPlatformSettings | null>(null);
  const [savingPlatform, setSavingPlatform] = useState<PublishPlatform | null>(null);
  const [socialNames, setSocialNames] = useState<Record<SocialPlatform, string>>({
    douyin: "creator",
    xiaohongshu: "creator",
    kuaishou: "creator",
  });
  const [socialFiles, setSocialFiles] = useState<Partial<Record<SocialPlatform, File | null>>>({});
  const [loginSessions, setLoginSessions] = useState<Partial<Record<SocialPlatform, SocialLoginSession>>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setError(null);
    try {
      const [s, a, accounts, platforms] = await Promise.all([
        fetchJson<PublishSettings>(`${BILIBILI_PUBLISHER_URL}/bilibili/publish/settings`),
        fetchJson<AuthSettings>(`${BILIBILI_PUBLISHER_URL}/bilibili/auth/settings`),
        fetchJson<SocialAccount[]>(`${ORCHESTRATOR_URL}/settings/publish/social/accounts`),
        fetchJson<PublishPlatformSettingsResponse>(`${ORCHESTRATOR_URL}/settings/publish/platforms`),
      ]);
      setSettings(s);
      setAuth(a);
      setMe(null);
      setSocialAccounts(accounts);
      setPlatformSettings(platforms.platforms);
      setMetaText(JSON.stringify(s.default_meta ?? {}, null, 2));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!socialAccounts.some((account) => account.check_state === "queued" || account.check_state === "checking")) return;
    const timer = window.setInterval(() => refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [socialAccounts]);

  useEffect(() => {
    const active = Object.values(loginSessions).filter(
      (session): session is SocialLoginSession => Boolean(session && ["starting", "running", "canceling"].includes(session.state)),
    );
    if (active.length === 0) return;
    const timer = window.setInterval(async () => {
      const updates = await Promise.all(
        active.map(async (session) => {
          try {
            return await fetchJson<SocialLoginSession>(
              `${ORCHESTRATOR_URL}/settings/publish/social/login-sessions/${session.id}`,
            );
          } catch {
            return session;
          }
        }),
      );
      setLoginSessions((current) => {
        const next = { ...current };
        for (const session of updates) next[session.platform] = session;
        return next;
      });
      if (updates.some((session) => session.state === "succeeded")) await refresh();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [loginSessions]);

  async function importSocialAccount(platform: SocialPlatform) {
    const file = socialFiles[platform];
    if (!file) throw new Error("请选择 storage_state JSON 文件");
    const form = new FormData();
    form.append("account_name", socialNames[platform].trim());
    form.append("file", file, file.name);
    await fetchJson(`${ORCHESTRATOR_URL}/settings/publish/social/accounts/${platform}`, { method: "POST", body: form });
    setSocialFiles((current) => ({ ...current, [platform]: null }));
    await refresh();
  }

  async function setPlatformEnabled(platform: PublishPlatform, enabled: boolean) {
    setSavingPlatform(platform);
    setError(null);
    try {
      const response = await fetchJson<PublishPlatformSettingsResponse>(
        `${ORCHESTRATOR_URL}/settings/publish/platforms/${platform}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        },
      );
      setPlatformSettings(response.platforms);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingPlatform(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">投稿设置</div>
        <div className="mt-1 text-sm text-slate-600">按平台配置投稿启用状态、登录凭据和默认投稿策略。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      {SOCIAL_PLATFORMS.map(({ id, label }) => {
        const guidance = storageStateGuidance(id, socialNames[id]);
        const accounts = activeAccountsForPlatform(socialAccounts, id);
        const loginSession = loginSessions[id];
        return (
          <div key={id} className="rounded border bg-white p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">{label}</div>
                <div className="mt-1 text-xs text-slate-500">
                  由独立 social-publisher 容器投稿；抖音可在任务详情中打开自动化窗口，其他平台使用无头 Chromium。
                </div>
              </div>
              <div className="flex items-center gap-3">
                <div className="rounded bg-slate-100 px-2 py-1 text-xs text-slate-600">SAU</div>
                <label className="flex items-center gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={Boolean(platformSettings?.[id])}
                    disabled={!platformSettings || savingPlatform !== null}
                    onChange={(event) => setPlatformEnabled(id, event.target.checked)}
                  />
                  {platformSettings?.[id] ? "已启用投稿" : "启用投稿"}
                </label>
              </div>
            </div>

            <div className="mt-3 rounded border bg-slate-50 p-3 text-xs text-slate-600">
              <div className="font-medium text-slate-800">推荐：网页登录</div>
              <div className="mt-1">
                VideoRoll 会打开一个临时浏览器窗口。扫码后如果出现短信、安全校验或授权确认，可直接在该窗口中继续操作。
              </div>
              <div className="mt-3 border-t pt-3">也可以继续使用本地 SAU 文件导入：</div>
              <div>请先在本地 social-auto-upload 目录执行：</div>
              <div className="mt-1 break-all font-mono text-slate-800">{guidance.command}</div>
              <div className="mt-2">登录成功后上传：</div>
              <div className="mt-1 break-all font-mono text-slate-800">{guidance.path}</div>
              <div className="mt-2 text-amber-700">
                仅接受 SAU 生成的 Playwright/Patchright storage_state JSON。普通 Cookie 字符串可能缺少 localStorage/origins，不能替代该文件。
              </div>
              <div className="mt-1">文件会使用 VideoRoll Fernet 密钥加密保存，网页不会回显内容，也不会上传到 S3。</div>
            </div>

            <div className="mt-3 grid gap-2 md:grid-cols-[14rem_auto_1fr_auto]">
              <input
                className="rounded border px-3 py-2 text-sm"
                value={socialNames[id]}
                onChange={(event) => setSocialNames((current) => ({ ...current, [id]: event.target.value }))}
                placeholder="账号名，例如 creator"
              />
              <button
                disabled={busy || Boolean(loginSession && ["starting", "running", "canceling"].includes(loginSession.state))}
                className="rounded bg-indigo-600 px-3 py-2 text-sm text-white disabled:opacity-50"
                onClick={async () => {
                  const popup = window.open("about:blank", `social-login-${id}`, "popup,width=1280,height=860,resizable=yes,scrollbars=yes");
                  setBusy(true);
                  setError(null);
                  try {
                    const session = await fetchJson<SocialLoginSession>(
                      `${ORCHESTRATOR_URL}/settings/publish/social/login-sessions/${id}`,
                      {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ account_name: socialNames[id].trim() }),
                      },
                    );
                    setLoginSessions((current) => ({ ...current, [id]: session }));
                    if (popup) popup.location.replace(new URL(session.browser_url, window.location.origin).toString());
                  } catch (e: unknown) {
                    popup?.close();
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                网页登录
              </button>
              <input
                type="file"
                accept="application/json,.json"
                className="rounded border px-3 py-2 text-sm"
                onChange={(event) => setSocialFiles((current) => ({ ...current, [id]: event.target.files?.[0] ?? null }))}
              />
              <button
                disabled={busy || !socialFiles[id] || !socialNames[id].trim()}
                className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await importSocialAccount(id);
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                导入并校验
              </button>
            </div>

            {loginSession ? (
              <div className={`mt-3 rounded border p-3 text-sm ${loginSession.state === "failed" ? "border-rose-300 bg-rose-50 text-rose-800" : "bg-indigo-50 text-indigo-900"}`}>
                <div>{loginSessionLabel(loginSession)}</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {loginSession.state === "running" ? (
                    <button
                      className="rounded border border-indigo-300 px-3 py-1.5 text-xs"
                      onClick={() => window.open(new URL(loginSession.browser_url, window.location.origin).toString(), `social-login-${id}`, "popup,width=1280,height=860,resizable=yes,scrollbars=yes")}
                    >
                      打开登录窗口
                    </button>
                  ) : null}
                  {["starting", "running", "canceling"].includes(loginSession.state) ? (
                    <button
                      className="rounded border border-rose-300 px-3 py-1.5 text-xs text-rose-700"
                      onClick={async () => {
                        const session = await fetchJson<SocialLoginSession>(
                          `${ORCHESTRATOR_URL}/settings/publish/social/login-sessions/${loginSession.id}`,
                          { method: "DELETE" },
                        );
                        setLoginSessions((current) => ({ ...current, [id]: session }));
                      }}
                    >
                      取消登录
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}

            <div className="mt-3 space-y-2">
              {accounts.length === 0 ? <div className="text-sm text-slate-500">尚未导入账号。</div> : null}
              {accounts.map((account) => (
                <div key={account.id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-3 text-sm">
                  <div>
                    <div className="font-medium">{account.name}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      状态：{account.check_state}
                      {account.last_checked_at ? ` · ${new Date(account.last_checked_at).toLocaleString()}` : ""}
                      {account.last_check_message ? ` · ${account.last_check_message}` : ""}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      disabled={busy}
                      className="rounded border px-3 py-1.5 text-xs disabled:opacity-50"
                      onClick={async () => {
                        setBusy(true);
                        setError(null);
                        try {
                          await fetchJson(`${ORCHESTRATOR_URL}/settings/publish/social/accounts/${account.id}/check`, { method: "POST" });
                          await refresh();
                        } catch (e: unknown) {
                          setError(e instanceof Error ? e.message : String(e));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      重新校验
                    </button>
                    <button
                      disabled={busy}
                      className="rounded border border-rose-300 px-3 py-1.5 text-xs text-rose-700 disabled:opacity-50"
                      onClick={async () => {
                        const ok = await confirm({
                          title: `删除 ${label} 账号`,
                          message: `确定删除账号 ${account.name} 的加密登录状态吗？`,
                          confirmLabel: "删除",
                          tone: "danger",
                        });
                        if (!ok) return;
                        setBusy(true);
                        setError(null);
                        try {
                          await fetchJson(`${ORCHESTRATOR_URL}/settings/publish/social/accounts/${account.id}`, { method: "DELETE" });
                          await refresh();
                        } catch (e: unknown) {
                          setError(e instanceof Error ? e.message : String(e));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}

      <div className="rounded border bg-white p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">哔哩哔哩</div>
            <div className="mt-1 text-xs text-slate-500">继续使用现有 Bilibili Web/API 投稿实现。</div>
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={Boolean(platformSettings?.bilibili)}
              disabled={!platformSettings || savingPlatform !== null}
              onChange={(event) => setPlatformEnabled("bilibili", event.target.checked)}
            />
            {platformSettings?.bilibili ? "已启用投稿" : "启用投稿"}
          </label>
        </div>

        <div className="mt-4 text-sm font-semibold">Cookies 登录</div>
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
              const ok = await confirm({
                title: "清除 Bilibili Cookies",
                message: "确定清除已保存的 Cookies 吗？清除后真实投稿不可用。",
                confirmLabel: "清除",
                tone: "danger",
              });
              if (!ok) return;
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
                  const ok = await confirm({
                    title: "恢复默认模板",
                    message: "确定恢复内置默认模板吗？当前 default_meta.json 会被覆盖。",
                    confirmLabel: "恢复默认",
                    tone: "warning",
                  });
                  if (!ok) return;
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
