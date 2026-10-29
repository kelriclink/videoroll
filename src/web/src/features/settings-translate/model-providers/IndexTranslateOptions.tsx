import type { ModelProviderDraft } from "../../../api/translate";
import { TechnicalDetails } from "../../../components/ui";
import { indexOptionsFromDraft, inputClass, labelClass } from "./modelProviderForm";

export function IndexTranslateOptions({
  draft,
  onOptionsChange,
}: {
  draft: ModelProviderDraft;
  onOptionsChange: (patch: Record<string, unknown>) => void;
}) {
  const options = indexOptionsFromDraft(draft);
  return (
    <TechnicalDetails summary="Index-Translate 高级参数">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={labelClass}>
          <span>每批字幕数</span>
          <input
            className={inputClass}
            type="number"
            min={1}
            max={30}
            value={options.batchSize}
            onChange={(event) => onOptionsChange({ batch_size: Number(event.target.value) })}
          />
        </label>
        <label className={labelClass}>
          <span>原文参考上限（字符）</span>
          <input
            className={inputClass}
            type="number"
            min={4000}
            max={100000}
            step={1000}
            value={options.contextChars}
            onChange={(event) => onOptionsChange({ context_chars: Number(event.target.value) })}
          />
        </label>
        <label className={labelClass + " sm:col-span-2"}>
          <span>内容领域</span>
          <input className={inputClass} value={options.domain} onChange={(event) => onOptionsChange({ domain: event.target.value })} />
        </label>
        <label className={labelClass + " sm:col-span-2"}>
          <span>固定术语</span>
          <textarea
            rows={2}
            className={inputClass}
            value={options.glossary}
            onChange={(event) => onOptionsChange({ glossary: event.target.value })}
            placeholder="PCB:印刷电路板, I2C:I2C"
          />
        </label>
        <label className={labelClass + " sm:col-span-2"}>
          <span>额外翻译要求</span>
          <textarea
            rows={3}
            className={inputClass}
            value={options.instruction}
            onChange={(event) => onOptionsChange({ instruction: event.target.value })}
          />
        </label>
      </div>
    </TechnicalDetails>
  );
}
