import { ReactNode, useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import AuthGate from "./components/AuthGate";
import { FeedbackProvider } from "./components/Feedback";
import { fetchJson } from "./lib/http";
import { ORCHESTRATOR_URL } from "./lib/urls";
import { RealtimeProvider } from "./lib/realtime";
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
import SettingsPublishPage from "./pages/SettingsPublishPage";
import SettingsAutoPage from "./pages/SettingsAutoPage";
import SettingsReviewPage from "./pages/SettingsReviewPage";
import LivePage from "./pages/LivePage";
import RenderQueuePage from "./pages/RenderQueuePage";
import KnowledgeBasePage from "./pages/KnowledgeBasePage";
import DictionaryPage from "./pages/DictionaryPage";

function NavItem({ to, label, onNavigate }: { to: string; label: string; onNavigate?: () => void }) {
  return (
    <NavLink
      to={to}
      onClick={onNavigate}
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

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="rounded-md border border-slate-200 bg-white p-2 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <NavGroup title="工作台">
        <NavItem to="/" label="仪表盘" onNavigate={onNavigate} />
        <NavItem to="/tasks" label="任务" onNavigate={onNavigate} />
        <NavItem to="/videos" label="视频成品" onNavigate={onNavigate} />
        <NavItem to="/live" label="直播推流" onNavigate={onNavigate} />
        <NavItem to="/queue/render" label="处理队列" onNavigate={onNavigate} />
        <NavItem to="/knowledge" label="知识库" onNavigate={onNavigate} />
        <NavItem to="/dictionaries" label="词典" onNavigate={onNavigate} />
      </NavGroup>
      <NavGroup title="来源">
        <NavItem to="/tasks/new" label="新建任务" onNavigate={onNavigate} />
        <NavItem to="/youtube/sources" label="YouTube 来源" onNavigate={onNavigate} />
      </NavGroup>
      <NavGroup title="配置">
        <NavItem to="/settings/auto" label="自动模式" onNavigate={onNavigate} />
        <NavItem to="/settings/youtube" label="YouTube" onNavigate={onNavigate} />
        <NavItem to="/settings/publish" label="投稿设置" onNavigate={onNavigate} />
        <NavItem to="/settings/asr" label="ASR" onNavigate={onNavigate} />
        <NavItem to="/settings/translate" label="翻译 / RAG" onNavigate={onNavigate} />
        <NavItem to="/settings/review" label="审核" onNavigate={onNavigate} />
        <NavItem to="/settings/storage" label="存储" onNavigate={onNavigate} />
        <NavItem to="/settings/api" label="API" onNavigate={onNavigate} />
      </NavGroup>
    </div>
  );
}

function NotFoundPage() {
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-5 text-slate-900">
      <div className="text-lg font-semibold">页面不存在</div>
      <div className="mt-1 text-sm text-slate-600">此地址没有对应功能，请返回仪表盘继续操作。</div>
      <Link to="/" className="mt-4 inline-block rounded-md border border-slate-300 bg-white px-3 py-2 text-sm hover:bg-slate-50">
        返回仪表盘
      </Link>
    </div>
  );
}

export default function App() {
  const location = useLocation();
  const orchestratorDisplay =
    ORCHESTRATOR_URL.startsWith("http://") || ORCHESTRATOR_URL.startsWith("https://")
      ? ORCHESTRATOR_URL
      : typeof window !== "undefined"
        ? `${window.location.origin}${ORCHESTRATOR_URL}`
        : ORCHESTRATOR_URL;

  const [loggingOut, setLoggingOut] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
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

  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

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
        <RealtimeProvider>
          <div className="min-h-screen bg-slate-50 transition-colors dark:bg-slate-950">
        <header className="border-b bg-white transition-colors dark:border-slate-800 dark:bg-slate-950">
          <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3">
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 md:hidden"
                aria-label="打开导航"
                title="打开导航"
                onClick={() => setMobileNavOpen(true)}
              >
                ☰
              </button>
              <Link to="/" className="font-semibold">
                VideoRoll
              </Link>
            </div>
            <div className="flex items-center gap-3">
              <div className="hidden text-xs text-slate-500 sm:block">合规处理台</div>
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
          <div className="mx-auto max-w-7xl px-4 pb-3 text-xs text-slate-600">
            仅用于处理你拥有版权/已获授权/允许再分发的内容。
          </div>
        </header>

        {mobileNavOpen ? (
          <div className="fixed inset-0 z-40 md:hidden">
            <button
              type="button"
              className="absolute inset-0 bg-slate-950/40"
              aria-label="关闭导航"
              onClick={() => setMobileNavOpen(false)}
            />
            <div className="relative h-full w-[min(20rem,calc(100vw-3rem))] overflow-auto bg-slate-50 p-4 shadow-xl dark:bg-slate-950">
              <div className="mb-3 flex items-center justify-between gap-3">
                <div className="font-semibold">VideoRoll</div>
                <button
                  type="button"
                  className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
                  onClick={() => setMobileNavOpen(false)}
                >
                  关闭
                </button>
              </div>
              <Navigation onNavigate={() => setMobileNavOpen(false)} />
              <div className="mt-3 rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
                后端：{orchestratorDisplay}
              </div>
            </div>
          </div>
        ) : null}

        <div className="mx-auto grid max-w-7xl grid-cols-12 gap-4 px-4 py-4">
          <aside className="hidden md:col-span-3 md:block lg:col-span-2">
            <Navigation />
            <div className="mt-3 rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              后端：{orchestratorDisplay}
            </div>
          </aside>

          <main className="col-span-12 md:col-span-9 lg:col-span-10">
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/videos" element={<VideosPage />} />
              <Route path="/live" element={<LivePage />} />
              <Route path="/tasks/new" element={<TaskNewPage />} />
              <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
              <Route path="/youtube/sources" element={<YouTubeSourcesPage />} />
              <Route path="/queue/render" element={<RenderQueuePage />} />
              <Route path="/knowledge" element={<KnowledgeBasePage />} />
              <Route path="/dictionaries" element={<DictionaryPage />} />
              <Route path="/settings/asr" element={<SettingsASRPage />} />
              <Route path="/settings/youtube" element={<SettingsYouTubePage />} />
              <Route path="/settings/storage" element={<SettingsStoragePage />} />
              <Route path="/settings/api" element={<SettingsApiPage />} />
              <Route path="/settings/review" element={<SettingsReviewPage />} />
              <Route path="/settings/auto" element={<SettingsAutoPage />} />
              <Route path="/settings/translate" element={<SettingsTranslatePage />} />
              <Route path="/settings/publish" element={<SettingsPublishPage />} />
              <Route path="/settings/bilibili" element={<SettingsPublishPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Routes>
          </main>
        </div>
          </div>
        </RealtimeProvider>
      </AuthGate>
    </FeedbackProvider>
  );
}
