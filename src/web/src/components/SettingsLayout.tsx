import { NavLink, Outlet } from "react-router-dom";

const settingsItems = [
  ["/settings/auto", "自动模式"],
  ["/settings/youtube", "YouTube"],
  ["/settings/publish", "投稿"],
  ["/settings/asr", "ASR"],
  ["/settings/translate", "翻译 / RAG"],
  ["/settings/review", "审核"],
  ["/settings/storage", "存储"],
  ["/settings/api", "API"],
] as const;

export default function SettingsLayout() {
  return (
    <div className="space-y-5">
      <div className="border-b border-slate-200 pb-3 dark:border-slate-800">
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <div className="text-xs font-medium text-slate-400">系统</div>
            <h1 className="mt-1 text-xl font-semibold tracking-tight text-slate-950 dark:text-white">设置</h1>
          </div>
          <div className="hidden text-xs text-slate-500 sm:block">修改后仅影响后续任务，除非页面另有说明</div>
        </div>
        <nav className="-mb-3 flex gap-1 overflow-x-auto pb-px md:hidden" aria-label="设置导航">
          {settingsItems.map(([to, label]) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                [
                  "shrink-0 border-b-2 px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "border-slate-950 font-medium text-slate-950 dark:border-slate-100 dark:text-white"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-900 dark:hover:text-slate-200",
                ].join(" ")
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </div>
      <Outlet />
    </div>
  );
}
