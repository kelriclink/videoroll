import { useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import AuthGate from "./components/AuthGate";
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
import RenderQueuePage from "./pages/RenderQueuePage";

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        [
          "block rounded px-3 py-2 text-sm",
          isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100",
        ].join(" ")
      }
    >
      {label}
    </NavLink>
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
    <AuthGate>
      <div className="min-h-screen bg-slate-50">
        <header className="border-b bg-white">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
            <Link to="/" className="font-semibold">
              VideoRoll
            </Link>
            <div className="flex items-center gap-3">
              <div className="text-xs text-slate-500">合规版 · MVP</div>
              <button
                type="button"
                onClick={logout}
                disabled={loggingOut}
                className={[
                  "rounded border px-2 py-1 text-xs",
                  loggingOut ? "cursor-not-allowed bg-slate-100 text-slate-400" : "hover:bg-slate-50",
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
            <div className="rounded border bg-white p-2">
              <NavItem to="/" label="Dashboard" />
              <NavItem to="/tasks" label="Tasks" />
              <NavItem to="/videos" label="Videos" />
              <NavItem to="/tasks/new" label="New Task" />
              <NavItem to="/youtube/sources" label="YouTube Sources" />
              <NavItem to="/queue/render" label="Queue · Task" />
              <NavItem to="/settings/asr" label="Settings · ASR" />
              <NavItem to="/settings/youtube" label="Settings · YouTube" />
              <NavItem to="/settings/storage" label="Settings · Storage" />
              <NavItem to="/settings/api" label="Settings · API" />
              <NavItem to="/settings/auto" label="Settings · Auto" />
              <NavItem to="/settings/translate" label="Settings · Translate" />
              <NavItem to="/settings/bilibili" label="Settings · Bilibili" />
            </div>
            <div className="mt-3 rounded border bg-white p-3 text-xs text-slate-600">
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
              <Route path="/settings/asr" element={<SettingsASRPage />} />
              <Route path="/settings/youtube" element={<SettingsYouTubePage />} />
              <Route path="/settings/storage" element={<SettingsStoragePage />} />
              <Route path="/settings/api" element={<SettingsApiPage />} />
              <Route path="/settings/auto" element={<SettingsAutoPage />} />
              <Route path="/settings/translate" element={<SettingsTranslatePage />} />
              <Route path="/settings/bilibili" element={<SettingsBilibiliPage />} />
            </Routes>
          </main>
        </div>
      </div>
    </AuthGate>
  );
}
