import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Section, TechnicalDetails } from "../../../components/ui";

export function TranslationTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    defaultProvider,
    setDefaultProvider,
    defaultTargetLang,
    setDefaultTargetLang,
    defaultStyle,
    setDefaultStyle,
    defaultBatchSize,
    setDefaultBatchSize,
    defaultMaxRetries,
    setDefaultMaxRetries,
    defaultEnableSummary,
    setDefaultEnableSummary,
  } = controller;

  return (
    <Section>
      <div>
        <div className="text-sm font-semibold text-slate-950 dark:text-white">通用翻译行为</div>
        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          这里设置翻译风格和默认行为；模型接口统一在上方“模型服务”管理，自动任务使用哪个模型在“自动处理”页面选择。
        </div>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="block">
          <div className="mb-1 text-xs text-slate-600 dark:text-slate-300">默认目标语言</div>
          <input
            className="vr-input w-full"
            value={defaultTargetLang}
            onChange={(event) => setDefaultTargetLang(event.target.value)}
            placeholder="zh"
          />
        </label>
        <label className="block">
          <div className="mb-1 text-xs text-slate-600 dark:text-slate-300">默认翻译风格</div>
          <select
            className="vr-input w-full"
            value={defaultStyle}
            onChange={(event) => setDefaultStyle(event.target.value)}
          >
            <option value="口语自然">口语自然</option>
            <option value="正式严谨">正式严谨</option>
            <option value="电商营销">电商营销</option>
          </select>
        </label>
      </div>

      <TechnicalDetails className="mt-4" summary="高级翻译参数">
        <div className="grid gap-3 sm:grid-cols-2">
          <label>
            <div className="mb-1 text-xs text-slate-500">每批字幕数</div>
            <input
              type="number"
              min={1}
              className="vr-input w-full"
              value={defaultBatchSize}
              onChange={(event) => setDefaultBatchSize(parseInt(event.target.value || "1", 10))}
            />
          </label>
          <label>
            <div className="mb-1 text-xs text-slate-500">单批最大重试次数</div>
            <input
              type="number"
              min={0}
              max={10}
              className="vr-input w-full"
              value={defaultMaxRetries}
              onChange={(event) => setDefaultMaxRetries(parseInt(event.target.value || "0", 10))}
            />
          </label>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              checked={defaultEnableSummary}
              onChange={(event) => setDefaultEnableSummary(event.target.checked)}
            />
            在长视频翻译批次之间保留摘要上下文
          </label>
        </div>
      </TechnicalDetails>

      <TechnicalDetails className="mt-3" summary="兼容默认值">
        <div className="text-xs text-slate-500">
          仅用于没有显式指定翻译服务的旧任务和诊断调用。自动处理与任务详情中的模型选择不读取这里。
        </div>
        <label className="mt-3 block">
          <div className="mb-1 text-xs text-slate-500">兼容默认翻译方式</div>
          <select
            className="vr-input w-full"
            value={defaultProvider === "index_translate" ? "index_translate" : "openai"}
            onChange={(event) => setDefaultProvider(event.target.value)}
          >
            <option value="openai">OpenAI 兼容模型</option>
            <option value="index_translate">Index-Translate 官方免费服务</option>
          </select>
        </label>
      </TechnicalDetails>
    </Section>
  );
}
