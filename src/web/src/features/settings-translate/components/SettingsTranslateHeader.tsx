import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Link } from "react-router-dom";
import { Button, PageHeader } from "../../../components/ui";

export function SettingsTranslateHeader({ controller }: { controller: SettingsTranslateController }) {
  const {
    error,
    refresh
  } = controller;
  return (
    <>
      <PageHeader
        title="Settings · Translate"
        description="配置 OpenAI 翻译、RAG Gate、pgvector 和 embedding 模型。"
        actions={
          <>
            <Link to="/knowledge" className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50">
              知识库
            </Link>
            <Button onClick={() => refresh()}>刷新</Button>
          </>
        }
      />
      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
    </>
  );
}
