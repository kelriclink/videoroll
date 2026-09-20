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
        <div className="text-sm font-semibold text-slate-900">翻译诊断</div>
        <div className="mt-1 text-xs text-slate-500">使用当前已保存的模型配置执行一次实际翻译请求，用于检查连接与输出。</div>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">目标语言</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={testTargetLang} onChange={(e) => setTestTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">翻译风格</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={testStyle} onChange={(e) => setTestStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">测试文本</div>
            <textarea className="h-28 w-full rounded border px-3 py-2 text-sm" value={testText} onChange={(e) => setTestText(e.target.value)} />
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
          {testResult ? <div className="text-sm text-slate-700">OK</div> : null}
        </div>
        {testResult ? (
          <div className="mt-3 rounded border bg-slate-50 p-3">
            <div className="text-xs text-slate-500">译文</div>
            <div className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{testResult}</div>
          </div>
        ) : null}
      </Section>
  );
}
