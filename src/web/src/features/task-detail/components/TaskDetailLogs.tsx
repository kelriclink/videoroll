import type { TaskDetailController } from "../useTaskDetailController";
export function TaskDetailLogs({ controller }: { controller: TaskDetailController }) {
  const {
    logSelection,
    setLogSelection,
    logText,
    logBusy,
    logError,
    logAssets,
    loadLogs
  } = controller;
  return (
<div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">Logs</div>
          <button
            disabled={logBusy}
            onClick={() => loadLogs()}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
          >
            刷新日志
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select className="rounded border px-3 py-2 text-sm" value={logSelection} onChange={(e) => setLogSelection(e.target.value)}>
            <option value="combined">合并（最新 YouTube 下载 + 字幕 + 压制）</option>
            {[...logAssets].reverse().map((a) => (
              <option key={a.id} value={a.id}>
                {a.storage_key}
              </option>
            ))}
          </select>
          <div className="text-xs text-slate-500">仅显示末尾 200KB；完整内容请在 Assets 里下载。</div>
        </div>
        {logError ? <div className="mt-2 text-xs text-rose-700">{logError}</div> : null}
        <textarea
          readOnly
          value={logText}
          className="mt-2 h-72 w-full rounded border bg-slate-50 p-2 font-mono text-xs text-slate-800"
        />
      </div>
  );
}
