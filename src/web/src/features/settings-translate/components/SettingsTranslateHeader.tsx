import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Link } from "react-router-dom";
import { Button } from "../../../components/ui";

export function SettingsTranslateHeader({ controller }: { controller: SettingsTranslateController }) {
  const {
    error,
    refresh
  } = controller;
  return (
    <>
      <div className="flex flex-col gap-3 px-1 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-slate-950">翻译 / RAG</h2>
          <div className="mt-1 text-sm text-slate-500">配置翻译模型、RAG Agent、Embedding 与诊断工具。</div>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/knowledge" className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white">
            知识库
          </Link>
          <Button onClick={() => refresh()}>刷新</Button>
        </div>
      </div>
      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
    </>
  );
}
