import { ReactNode, useEffect, useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import AuthGate from "./components/AuthGate";
import { FeedbackProvider } from "./components/Feedback";
import { fetchJson } from "./lib/http";
import { ORCHESTRATOR_URL } from "./lib/urls";
import DashboardPage from "./pages/DashboardPage";
import TaskDetailPage from "./pages/TaskDetailPage";
import TaskNewPage from "./pages/TaskNewPage";
import TasksPage from "./pages/TasksPage";
import VideosPage from "./pages/VideosPage";
import YouTubeSourcesPage from "./pages/YouTubeSourcesPage";
import SettingsASRPage from "./pages/SettingsASRPage";
import SettingsYouTubePage from "./pages/SettingsYouTubePage";
import SettingsStoragePage from "./pages/SettingsStoragePage";
import SettingsApiPage from "./pages/SettingsApiPage";
import SettingsTranslatePage from "./pages/SettingsTranslatePage";
import SettingsBilibiliPage from "./pages/SettingsBilibiliPage";
import SettingsAutoPage from "./pages/SettingsAutoPage";
import SettingsReviewPage from "./pages/SettingsReviewPage";
import RenderQueuePage from "./pages/RenderQueuePage";
import KnowledgeBasePage from "./pages/KnowledgeBasePage";

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        [
          "block rounded-md px-3 py-2 text-sm",
          isActive
            ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
            : "text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
        ].join(" ")
      }
    >
      {label}
    </NavLink>
  );
}

function NavGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-normal text-slate-400">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

export default function App() {
  const orchestratorDisplay =
    ORCHESTRATOR_URL.startsWith("http://") || ORCHESTRATOR_URL.startsWith("https://")
      ? ORCHESTRATOR_URL
      : typeof window !== "undefined"
        ? `${window.location.origin}${ORCHESTRATOR_URL}`
        : ORCHESTRATOR_URL;

  const [loggingOut, setLoggingOut] = useState(false);
  const [darkMode, setDarkMode] = useState(() => {
    if (typeof window === "undefined") return false;
    const stored = window.localStorage.getItem("videoroll-theme");
    if (stored === "dark") return true;
    if (stored === "light") return false;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  });

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", darkMode);
    window.localStorage.setItem("videoroll-theme", darkMode ? "dark" : "light");
  }, [darkMode]);

  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/auth/logout`, { method: "POST" });
      window.location.reload();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`退出失败：${msg}`);
    } finally {
      setLoggingOut(false);
    }
  }

  return (
    <FeedbackProvider>
      <AuthGate>
      <div className="min-h-screen bg-slate-50 transition-colors dark:bg-slate-950">
        <header className="border-b bg-white transition-colors dark:border-slate-800 dark:bg-slate-950">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
            <Link to="/" className="font-semibold">
              VideoRoll
            </Link>
            <div className="flex items-center gap-3">
              <div className="text-xs text-slate-500">合规版 · MVP</div>
              <button
                type="button"
                onClick={() => setDarkMode((value) => !value)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-900"
                title={darkMode ? "切换到浅色模式" : "切换到黑暗模式"}
                aria-label={darkMode ? "切换到浅色模式" : "切换到黑暗模式"}
              >
                {darkMode ? "☀" : "☾"}
              </button>
              <button
                type="button"
                onClick={logout}
                disabled={loggingOut}
                className={[
                  "rounded border px-2 py-1 text-xs dark:border-slate-700 dark:text-slate-200",
                  loggingOut ? "cursor-not-allowed bg-slate-100 text-slate-400 dark:bg-slate-900 dark:text-slate-500" : "hover:bg-slate-50 dark:hover:bg-slate-900",
                ].join(" ")}
              >
                {loggingOut ? "退出中…" : "退出"}
              </button>
            </div>
          </div>
          <div className="mx-auto max-w-6xl px-4 pb-3 text-xs text-slate-600">
            仅用于处理你拥有版权/已获授权/允许再分发的内容。
          </div>
        </header>

        <div className="mx-auto grid max-w-6xl grid-cols-12 gap-4 px-4 py-4">
          <aside className="col-span-12 md:col-span-3">
            <div className="rounded-md border border-slate-200 bg-white p-2 shadow-sm dark:border-slate-800 dark:bg-slate-900">
              <NavGroup title="工作台">
                <NavItem to="/" label="Dashboard" />
                <NavItem to="/tasks" label="Tasks" />
                <NavItem to="/videos" label="Videos" />
                <NavItem to="/queue/render" label="Queue · Task" />
                <NavItem to="/knowledge" label="Knowledge Base" />
              </NavGroup>
              <NavGroup title="来源">
                <NavItem to="/tasks/new" label="New Task" />
                <NavItem to="/youtube/sources" label="YouTube Sources" />
              </NavGroup>
              <NavGroup title="配置">
                <NavItem to="/settings/auto" label="Settings · Auto" />
                <NavItem to="/settings/youtube" label="Settings · YouTube" />
                <NavItem to="/settings/bilibili" label="Settings · Bilibili" />
                <NavItem to="/settings/asr" label="Settings · ASR" />
                <NavItem to="/settings/translate" label="Settings · Translate" />
                <NavItem to="/settings/review" label="Settings · Review" />
                <NavItem to="/settings/storage" label="Settings · Storage" />
                <NavItem to="/settings/api" label="Settings · API" />
              </NavGroup>
            </div>
            <div className="mt-3 rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              后端（Orchestrator）：{orchestratorDisplay}
            </div>
          </aside>

          <main className="col-span-12 md:col-span-9">
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/videos" element={<VideosPage />} />
              <Route path="/tasks/new" element={<TaskNewPage />} />
              <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
              <Route path="/youtube/sources" element={<YouTubeSourcesPage />} />
              <Route path="/queue/render" element={<RenderQueuePage />} />
              <Route path="/knowledge" element={<KnowledgeBasePage />} />
              <Route path="/settings/asr" element={<SettingsASRPage />} />
              <Route path="/settings/youtube" element={<SettingsYouTubePage />} />
              <Route path="/settings/storage" element={<SettingsStoragePage />} />
              <Route path="/settings/api" element={<SettingsApiPage />} />
              <Route path="/settings/review" element={<SettingsReviewPage />} />
              <Route path="/settings/auto" element={<SettingsAutoPage />} />
              <Route path="/settings/translate" element={<SettingsTranslatePage />} />
              <Route path="/settings/bilibili" element={<SettingsBilibiliPage />} />
            </Routes>
          </main>
        </div>
      </div>
      </AuthGate>
    </FeedbackProvider>
  );
}
