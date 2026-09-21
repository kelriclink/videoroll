import { Suspense, lazy, type ReactNode, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import AuthGate from "./components/AuthGate";
import { FeedbackProvider } from "./components/Feedback";
import SettingsLayout from "./components/SettingsLayout";
import { fetchJson } from "./lib/http";
import { ORCHESTRATOR_URL } from "./lib/urls";
import { RealtimeProvider } from "./lib/RealtimeProvider";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const TaskDetailPage = lazy(() => import("./pages/TaskDetailPage"));
const TaskNewPage = lazy(() => import("./pages/TaskNewPage"));
const TasksPage = lazy(() => import("./pages/TasksPage"));
const VideosPage = lazy(() => import("./pages/VideosPage"));
const YouTubeSourcesPage = lazy(() => import("./pages/YouTubeSourcesPage"));
const SettingsASRPage = lazy(() => import("./pages/SettingsASRPage"));
const SettingsYouTubePage = lazy(() => import("./pages/SettingsYouTubePage"));
const SettingsStoragePage = lazy(() => import("./pages/SettingsStoragePage"));
const SettingsApiPage = lazy(() => import("./pages/SettingsApiPage"));
const SettingsTranslatePage = lazy(() => import("./pages/SettingsTranslatePage"));
const SettingsPublishPage = lazy(() => import("./pages/SettingsPublishPage"));
const SettingsAutoPage = lazy(() => import("./pages/SettingsAutoPage"));
const SettingsReviewPage = lazy(() => import("./pages/SettingsReviewPage"));
const PlayoutPage = lazy(() => import("./pages/PlayoutPage"));
const RenderQueuePage = lazy(() => import("./pages/RenderQueuePage"));
const RenderManagementPage = lazy(() => import("./pages/RenderManagementPage"));
const KnowledgeBasePage = lazy(() => import("./pages/KnowledgeBasePage"));
const DictionaryPage = lazy(() => import("./pages/DictionaryPage"));
const OperationsPage = lazy(() => import("./pages/OperationsPage"));

function NavItem({ to, label, onNavigate }: { to: string; label: string; onNavigate?: () => void }) {
  return (
    <NavLink
      to={to}
      onClick={onNavigate}
      className={({ isActive }) =>
        [
          "relative flex min-h-9 items-center rounded-lg px-3 py-2 text-sm transition-colors",
          isActive
            ? "bg-slate-100 font-medium text-slate-950 before:absolute before:bottom-2 before:left-0 before:top-2 before:w-0.5 before:rounded-full before:bg-slate-900 dark:bg-slate-800 dark:text-white dark:before:bg-slate-100"
            : "text-slate-600 hover:bg-slate-100/80 hover:text-slate-950 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100",
        ].join(" ")
      }
    >
      {label}
    </NavLink>
  );
}

function NavGroup({
  title,
  children,
  collapsible = false,
  open = true,
  onToggle,
}: {
  title: string;
  children: ReactNode;
  collapsible?: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  return (
    <div className="space-y-1">
      {collapsible ? (
        <button
          type="button"
          className="flex w-full items-center justify-between rounded-md px-3 py-1.5 text-[11px] font-semibold text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300"
          aria-expanded={open}
          onClick={onToggle}
        >
          <span>{title}</span>
          <span aria-hidden="true" className="text-[10px]">{open ? "−" : "+"}</span>
        </button>
      ) : (
        <div className="px-3 py-1.5 text-[11px] font-semibold text-slate-400">{title}</div>
      )}
      {open ? <div className="space-y-0.5">{children}</div> : null}
    </div>
  );
}

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  const location = useLocation();
  const settingsActive = location.pathname.startsWith("/settings/");
  const [settingsOpen, setSettingsOpen] = useState(settingsActive);

  useEffect(() => {
    if (settingsActive) setSettingsOpen(true);
  }, [settingsActive]);

  return (
    <nav className="space-y-3" aria-label="主导航">
      <Link
        to="/tasks/new"
        onClick={onNavigate}
        className="flex items-center justify-center rounded-lg bg-slate-900 px-3 py-2 text-sm font-medium text-white transition hover:bg-slate-800 dark:bg-slate-100 dark:text-slate-950 dark:hover:bg-white"
      >
        ＋ 新建任务
      </Link>
      <NavGroup title="工作台">
        <NavItem to="/" label="工作台" onNavigate={onNavigate} />
        <NavItem to="/tasks" label="任务" onNavigate={onNavigate} />
        <NavItem to="/videos" label="视频成品" onNavigate={onNavigate} />
        <NavItem to="/playout" label="播控中心" onNavigate={onNavigate} />
        <NavItem to="/queue/render" label="处理队列" onNavigate={onNavigate} />
        <NavItem to="/render" label="渲染管理" onNavigate={onNavigate} />
        <NavItem to="/knowledge" label="知识库" onNavigate={onNavigate} />
        <NavItem to="/dictionaries" label="词典" onNavigate={onNavigate} />
        <NavItem to="/operations" label="运维中心" onNavigate={onNavigate} />
      </NavGroup>
      <NavGroup title="来源">
        <NavItem to="/youtube/sources" label="YouTube 来源" onNavigate={onNavigate} />
      </NavGroup>
      <NavGroup title="配置" collapsible open={settingsOpen} onToggle={() => setSettingsOpen((value) => !value)}>
        <NavItem to="/settings/auto" label="自动模式" onNavigate={onNavigate} />
        <NavItem to="/settings/youtube" label="YouTube" onNavigate={onNavigate} />
        <NavItem to="/settings/publish" label="投稿设置" onNavigate={onNavigate} />
        <NavItem to="/settings/asr" label="ASR" onNavigate={onNavigate} />
        <NavItem to="/settings/translate" label="翻译 / RAG" onNavigate={onNavigate} />
        <NavItem to="/settings/review" label="审核" onNavigate={onNavigate} />
        <NavItem to="/settings/storage" label="存储" onNavigate={onNavigate} />
        <NavItem to="/settings/api" label="API" onNavigate={onNavigate} />
      </NavGroup>
    </nav>
  );
}

function PageLoading() {
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      <div className="h-7 w-40 animate-pulse rounded bg-slate-200 dark:bg-slate-800" />
      <div className="h-4 w-72 max-w-full animate-pulse rounded bg-slate-200 dark:bg-slate-800" />
      <div className="mt-5 h-44 animate-pulse rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900" />
      <span className="sr-only">页面加载中</span>
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
  const isPlayoutRoute = location.pathname === "/playout";
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

  useEffect(() => {
    if (!mobileNavOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [mobileNavOpen]);

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
      <div
        className={[
          "bg-slate-50 transition-colors dark:bg-slate-950",
          isPlayoutRoute ? "flex h-screen flex-col overflow-hidden" : "min-h-screen",
        ].join(" ")}
      >
        <header
          className={[
            "sticky top-0 z-30 border-b bg-white/95 backdrop-blur transition-colors dark:border-slate-800 dark:bg-slate-950/95",
            isPlayoutRoute ? "flex-none" : "",
          ].join(" ")}
        >
          <div className="mx-auto flex max-w-[1440px] items-center justify-between px-4 py-3 lg:px-6">
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
              <Link to="/" className="text-[15px] font-semibold tracking-tight text-slate-950 dark:text-white">
                VideoRoll
              </Link>
              <span className="hidden rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500 dark:bg-slate-800 dark:text-slate-400 sm:inline-flex">
                合规处理台
              </span>
            </div>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setDarkMode((value) => !value)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-950 dark:border-slate-800 dark:text-slate-300 dark:hover:bg-slate-900"
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
                  "rounded-lg border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 dark:border-slate-800 dark:text-slate-300",
                  loggingOut ? "cursor-not-allowed bg-slate-100 text-slate-400 dark:bg-slate-900 dark:text-slate-500" : "hover:bg-slate-50 dark:hover:bg-slate-900",
                ].join(" ")}
              >
                {loggingOut ? "退出中…" : "退出"}
              </button>
            </div>
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
            <div
              className="relative h-full w-[min(20rem,calc(100vw-3rem))] overflow-auto bg-slate-50 p-4 shadow-xl dark:bg-slate-950"
              role="dialog"
              aria-modal="true"
              aria-label="主导航"
            >
              <div className="mb-4 flex items-center justify-between gap-3 border-b border-slate-200 pb-3 dark:border-slate-800">
                <div>
                  <div className="font-semibold">VideoRoll</div>
                  <div className="mt-0.5 text-xs text-slate-500">合规处理台</div>
                </div>
                <button
                  type="button"
                  className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
                  onClick={() => setMobileNavOpen(false)}
                >
                  关闭
                </button>
              </div>
              <Navigation onNavigate={() => setMobileNavOpen(false)} />
              <div className="mt-5 flex items-center gap-2 px-3 py-2 text-xs text-slate-500" title={orchestratorDisplay}>
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                <span>后端已连接</span>
              </div>
            </div>
          </div>
        ) : null}

        <div
          className={[
            "mx-auto grid max-w-[1440px] grid-cols-1 gap-6 px-4 py-5 md:grid-cols-[220px_minmax(0,1fr)] lg:px-6",
            isPlayoutRoute ? "min-h-0 w-full flex-1 overflow-hidden" : "",
          ].join(" ")}
        >
          <aside
            className={[
              "hidden md:block",
              isPlayoutRoute ? "min-h-0 overflow-y-auto" : "",
            ].join(" ")}
          >
            <div className="sticky top-[69px] max-h-[calc(100vh-89px)] overflow-y-auto pr-2">
              <Navigation />
              <div className="mt-5 flex items-center gap-2 px-3 py-2 text-xs text-slate-500" title={orchestratorDisplay}>
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                <span>后端已连接</span>
              </div>
            </div>
          </aside>

          <main
            className={[
              "min-w-0",
              isPlayoutRoute ? "min-h-0 overflow-hidden" : "",
            ].join(" ")}
          >
            <Suspense fallback={<PageLoading />}>
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/tasks" element={<TasksPage />} />
                <Route path="/videos" element={<VideosPage />} />
                <Route path="/live" element={<Navigate to="/playout" replace />} />
                <Route path="/playout" element={<PlayoutPage />} />
                <Route path="/tasks/new" element={<TaskNewPage />} />
                <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                <Route path="/youtube/sources" element={<YouTubeSourcesPage />} />
                <Route path="/queue/render" element={<RenderQueuePage />} />
                <Route path="/render" element={<RenderManagementPage />} />
                <Route path="/knowledge" element={<KnowledgeBasePage />} />
                <Route path="/dictionaries" element={<DictionaryPage />} />
                <Route path="/operations" element={<OperationsPage />} />
                <Route path="/settings" element={<SettingsLayout />}>
                  <Route index element={<Navigate to="auto" replace />} />
                  <Route path="auto" element={<SettingsAutoPage />} />
                  <Route path="youtube" element={<SettingsYouTubePage />} />
                  <Route path="publish" element={<SettingsPublishPage />} />
                  <Route path="bilibili" element={<SettingsPublishPage />} />
                  <Route path="asr" element={<SettingsASRPage />} />
                  <Route path="translate" element={<SettingsTranslatePage />} />
                  <Route path="review" element={<SettingsReviewPage />} />
                  <Route path="storage" element={<SettingsStoragePage />} />
                  <Route path="api" element={<SettingsApiPage />} />
                </Route>
                <Route path="*" element={<NotFoundPage />} />
              </Routes>
            </Suspense>
          </main>
        </div>
          </div>
        </RealtimeProvider>
      </AuthGate>
    </FeedbackProvider>
  );
}
