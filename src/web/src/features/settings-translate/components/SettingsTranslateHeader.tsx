import { Link } from "react-router-dom";
import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, TechnicalDetails } from "../../../components/ui";

export function SettingsTranslateHeader({ controller }: { controller: SettingsTranslateController }) {
  const { error, refresh } = controller;
  return (
    <>
      <div className="flex flex-col gap-3 px-1 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold tracking-tight text-slate-950 dark:text-white">翻译与 AI</h2>
          <div className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            管理翻译模型服务、知识增强和向量索引。自动流水线的模型选择在“自动处理”中单独配置。
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link
            to="/knowledge"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
          >
            知识库
          </Link>
          <Button onClick={() => refresh()}>刷新</Button>
        </div>
      </div>
      {error ? (
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300">
          配置读取或保存失败。
          <TechnicalDetails className="mt-2">
            <pre className="whitespace-pre-wrap break-words font-mono">{error}</pre>
          </TechnicalDetails>
        </div>
      ) : null}
    </>
  );
}
