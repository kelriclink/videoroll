import { PropsWithChildren, useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type AuthStatus = {
  password_set: boolean;
  trusted: boolean;
};

function clampError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err ?? "");
  const s = msg.trim();
  if (!s) return "Unknown error";
  if (s.length > 400) return s.slice(0, 399) + "…";
  return s;
}

async function getAuthStatus(): Promise<AuthStatus> {
  return await fetchJson<AuthStatus>(`${ORCHESTRATOR_URL}/auth/status`);
}

async function setupPassword(password: string): Promise<AuthStatus> {
  return await fetchJson<AuthStatus>(`${ORCHESTRATOR_URL}/auth/setup`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}

async function login(password: string): Promise<AuthStatus> {
  return await fetchJson<AuthStatus>(`${ORCHESTRATOR_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}

export default function AuthGate({ children }: PropsWithChildren) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const passwordSet = Boolean(status?.password_set);
  const trusted = Boolean(status?.trusted);

  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const mode = useMemo<"setup" | "login" | "app">(() => {
    if (!status) return "login";
    if (!status.password_set) return "setup";
    if (!status.trusted) return "login";
    return "app";
  }, [status]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError("");
    getAuthStatus()
      .then((s) => {
        if (!alive) return;
        setStatus(s);
        setLoading(false);
      })
      .catch((e) => {
        if (!alive) return;
        setLoadError(clampError(e));
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function submit() {
    setSubmitError("");
    setSubmitting(true);
    try {
      if (mode === "setup") {
        if (password.length < 8) throw new Error("Password too short (min 8 chars)");
        if (password !== password2) throw new Error("Passwords do not match");
        const s = await setupPassword(password);
        setStatus(s);
      } else {
        const s = await login(password);
        setStatus(s);
      }
      setPassword("");
      setPassword2("");
    } catch (e) {
      setSubmitError(clampError(e));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-50">
        <div className="mx-auto max-w-xl px-4 py-16">
          <div className="rounded border bg-white p-6 text-sm text-slate-700">Loading…</div>
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="min-h-screen bg-slate-50">
        <div className="mx-auto max-w-xl px-4 py-16">
          <div className="rounded border bg-white p-6">
            <div className="text-lg font-semibold">Failed to load</div>
            <div className="mt-2 text-sm text-red-700">{loadError}</div>
          </div>
        </div>
      </div>
    );
  }

  if (passwordSet && trusted) {
    return <>{children}</>;
  }

  const title = mode === "setup" ? "Set admin password" : "Admin login";
  const subtitle =
    mode === "setup"
      ? "首次使用/升级后需要设置管理密码。设置后当前设备将被记住。"
      : "此设备未被信任，请输入管理密码继续。";

  return (
    <div className="min-h-screen bg-slate-50">
      <div className="mx-auto max-w-xl px-4 py-16">
        <div className="rounded border bg-white p-6 shadow-sm">
          <div className="text-xl font-semibold text-slate-900">{title}</div>
          <div className="mt-2 text-sm text-slate-600">{subtitle}</div>

          <div className="mt-6">
            <label className="block text-sm font-medium text-slate-700">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded border px-3 py-2 text-sm outline-none focus:border-slate-500"
              placeholder="••••••••"
              autoFocus
            />
          </div>

          {mode === "setup" ? (
            <div className="mt-4">
              <label className="block text-sm font-medium text-slate-700">Confirm password</label>
              <input
                type="password"
                value={password2}
                onChange={(e) => setPassword2(e.target.value)}
                className="mt-1 w-full rounded border px-3 py-2 text-sm outline-none focus:border-slate-500"
                placeholder="••••••••"
              />
            </div>
          ) : null}

          {submitError ? <div className="mt-4 text-sm text-red-700">{submitError}</div> : null}

          <button
            type="button"
            disabled={submitting || !password}
            onClick={submit}
            className={[
              "mt-6 inline-flex w-full items-center justify-center rounded px-4 py-2 text-sm font-medium",
              submitting || !password
                ? "cursor-not-allowed bg-slate-200 text-slate-500"
                : "bg-slate-900 text-white hover:bg-slate-800",
            ].join(" ")}
          >
            {submitting ? "Please wait…" : mode === "setup" ? "Set password" : "Login"}
          </button>

          <div className="mt-4 text-xs text-slate-500">
            提示：此登录是“设备记住”模式；新设备/清理浏览器数据后需要重新输入密码。
          </div>
        </div>
      </div>
    </div>
  );
}

