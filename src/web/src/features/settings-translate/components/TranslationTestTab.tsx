import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Button, Section } from "../../../components/ui";
export function TranslationTestTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    busy,
    testText,
    setTestText,
    testTargetLang,
    setTestTargetLang,
    testStyle,
    setTestStyle,
    testResult,
    testTranslation,
  } = controller;
  return (
<Section>
        <div className="text-sm font-semibold text-slate-900 dark:text-white">翻译诊断</div>
        <div className="mt-1 text-xs text-slate-500">使用“自动处理”当前选中的翻译服务执行一次真实请求，用于确认生产任务实际会使用的模型能够连接并正常输出。</div>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">目标语言</div>
            <input className="vr-input w-full" value={testTargetLang} onChange={(e) => setTestTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">翻译风格</div>
            <select className="vr-input w-full" value={testStyle} onChange={(e) => setTestStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">测试文本</div>
            <textarea className="vr-input h-28 w-full" value={testText} onChange={(e) => setTestText(e.target.value)} />
          </label>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <Button
            tone="primary"
            disabled={busy}
            onClick={testTranslation}
          >
            {busy ? "测试中..." : "开始测试"}
          </Button>
          {testResult ? <div className="text-sm text-emerald-700 dark:text-emerald-300">连接与翻译正常</div> : null}
        </div>
        {testResult ? (
          <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-950">
            <div className="text-xs text-slate-500">译文</div>
            <div className="mt-1 whitespace-pre-wrap text-sm text-slate-800 dark:text-slate-200">{testResult}</div>
          </div>
        ) : null}
      </Section>
  );
}
