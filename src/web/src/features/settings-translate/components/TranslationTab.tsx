import type { SettingsTranslateController } from "../useSettingsTranslateController";
import { Section } from "../../../components/ui";
export function TranslationTab({ controller }: { controller: SettingsTranslateController }) {
  const {
    settings,
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
    indexSettings,
    setIndexSettings,
    openaiBaseUrl,
    setOpenaiBaseUrl,
    openaiModel,
    setOpenaiModel,
    openaiTemperature,
    setOpenaiTemperature,
    openaiTimeoutSeconds,
    setOpenaiTimeoutSeconds,
    openaiMaxRetries,
    setOpenaiMaxRetries,
    openaiApiType,
    setOpenaiApiType,
    openaiEnableThinking,
    setOpenaiEnableThinking,
    cerebrasReasoningEffort,
    setCerebrasReasoningEffort,
    cerebrasReasoningFormat,
    setCerebrasReasoningFormat,
    openaiApiKey,
    setOpenaiApiKey
  } = controller;
  return (
<Section>
        <div className="text-sm font-semibold text-slate-900">翻译与模型</div>
        <div className="mt-1 text-xs text-slate-500">默认翻译行为与 LLM 接口集中在这里；RAG 和 Embedding 使用各自独立页面。</div>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <div className="lg:col-span-2 border-b border-slate-100 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">默认翻译</div>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">默认 Provider</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultProvider} onChange={(e) => setDefaultProvider(e.target.value)}>
              <option value="openai">OpenAI / RAG</option>
              <option value="index_translate">Index-Translate 官方免费 API</option>
              <option value="mock">mock</option>
              <option value="noop">noop</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">目标语言</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={defaultTargetLang} onChange={(e) => setDefaultTargetLang(e.target.value)} />
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">默认翻译风格</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultStyle} onChange={(e) => setDefaultStyle(e.target.value)}>
              <option value="口语自然">口语自然</option>
              <option value="正式严谨">正式严谨</option>
              <option value="电商营销">电商营销</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">Batch 大小</div>
            <input type="number" min={1} disabled={defaultProvider === "index_translate"} className="w-full rounded border px-3 py-2 text-sm" value={defaultBatchSize} onChange={(e) => setDefaultBatchSize(parseInt(e.target.value || "1", 10))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">翻译批次最大重试</div>
            <input type="number" min={0} max={10} className="w-full rounded border px-3 py-2 text-sm" value={defaultMaxRetries} onChange={(e) => setDefaultMaxRetries(parseInt(e.target.value || "0", 10))} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" disabled={defaultProvider === "index_translate"} checked={defaultEnableSummary} onChange={(e) => setDefaultEnableSummary(e.target.checked)} />
            启用批次摘要上下文
          </label>
          {defaultProvider === "index_translate" ? (
            <div className="lg:col-span-2 grid gap-3 rounded-lg border border-sky-200 bg-sky-50 p-4 lg:grid-cols-2">
              <div className="lg:col-span-2 text-sm font-semibold text-sky-950">Index-Translate 翻译配置</div>
              <div className="lg:col-span-2 text-xs text-sky-800">
                官方免费服务 · Index-Translate-35B-A3B · https://index-translate.bilibili.com/v1
                <div className="mt-1">无需密钥。启用后字幕、视频标题只调用此翻译服务；不调用 OpenAI/RAG Agent。完整原文在上下文预算内作为软性参考，超出预算时采用片头、局部与片尾上下文。</div>
              </div>
              <label className="block">
                <div className="mb-1 text-xs">温度</div>
                <input type="number" min={0} max={2} step={0.1} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.temperature} onChange={e => setIndexSettings({temperature: Number(e.target.value)})} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs">最大输出 Token</div>
                <input type="number" min={128} max={16384} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.maxTokens} onChange={e => setIndexSettings({maxTokens: Number(e.target.value)})} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs">Index 每批字幕数</div>
                <input type="number" min={1} max={30} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.batchSize} onChange={e => setIndexSettings({batchSize: Number(e.target.value)})} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs">请求超时（秒）</div>
                <input type="number" min={15} max={300} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.timeoutSeconds} onChange={e => setIndexSettings({timeoutSeconds: Number(e.target.value)})} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs">完整字幕参考上限（字符）</div>
                <input type="number" min={4000} max={100000} step={1000} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.contextChars} onChange={e => setIndexSettings({contextChars: Number(e.target.value)})} />
              </label>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs">字幕领域</div>
                <input className="w-full rounded px-3 py-2 text-sm" value={indexSettings.domain} onChange={e => setIndexSettings({domain: e.target.value})} placeholder="如 科技视频字幕" />
              </label>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs">固定术语（硬性要求）</div>
                <textarea rows={2} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.glossary} onChange={e => setIndexSettings({glossary: e.target.value})} placeholder="PCB:印刷电路板, I2C:I2C" />
              </label>
              <label className="block lg:col-span-2">
                <div className="mb-1 text-xs">自定义翻译要求（软性要求）</div>
                <textarea rows={3} className="w-full rounded px-3 py-2 text-sm" value={indexSettings.instruction} onChange={e => setIndexSettings({instruction: e.target.value})} placeholder="保持科技视频解说风格，尽量精简句子" />
              </label>
            </div>
          ) : (
          <>
          <div className="lg:col-span-2 mt-1 border-b border-slate-100 pb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">模型接口（OpenAI）</div>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">接口类型</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={openaiApiType}
              onChange={(e) => setOpenaiApiType(e.target.value as "openai" | "cerebras")}
            >
              <option value="openai">OpenAI 标准兼容接口</option>
              <option value="cerebras">Cerebras（gpt-oss / reasoning）</option>
            </select>
            <div className="mt-1 text-xs text-slate-500">
              通过 New API 转发 Cerebras 时也请选择 Cerebras；系统会使用 Cerebras 的 reasoning 参数，而不是 <code>enable_thinking</code>。
            </div>
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_api_key（仅保存，不回显）</div>
            <input type="password" className="w-full rounded border px-3 py-2 text-sm" placeholder={settings?.openai_api_key_set ? "已设置（留空则不修改）" : "sk-..."} value={openaiApiKey} onChange={(e) => setOpenaiApiKey(e.target.value)} />
          </label>
          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openai_base_url</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiBaseUrl} onChange={(e) => setOpenaiBaseUrl(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_model</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={openaiModel} onChange={(e) => setOpenaiModel(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_temperature</div>
            <input type="number" step="0.1" min={0} max={2} className="w-full rounded border px-3 py-2 text-sm" value={openaiTemperature} onChange={(e) => setOpenaiTemperature(parseFloat(e.target.value || "0"))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_timeout_seconds</div>
            <input type="number" min={1} className="w-full rounded border px-3 py-2 text-sm" value={openaiTimeoutSeconds} onChange={(e) => setOpenaiTimeoutSeconds(parseFloat(e.target.value || "1"))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openai_max_retries</div>
            <input type="number" min={1} max={10} className="w-full rounded border px-3 py-2 text-sm" value={openaiMaxRetries} onChange={(e) => setOpenaiMaxRetries(parseInt(e.target.value || "3", 10))} />
            <div className="mt-1 text-xs text-slate-500">LLM 请求的网络/5xx 重试次数；不影响 embedding 请求。</div>
          </label>
          {openaiApiType === "cerebras" ? (
            <div className="grid gap-3 rounded border border-orange-200 bg-orange-50 p-3 lg:col-span-2 lg:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-orange-900">cerebras_reasoning_effort</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={cerebrasReasoningEffort}
                  onChange={(e) => setCerebrasReasoningEffort(e.target.value as "low" | "medium" | "high")}
                >
                  <option value="low">low（更快）</option>
                  <option value="medium">medium（推荐）</option>
                  <option value="high">high（更充分）</option>
                </select>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-orange-900">cerebras_reasoning_format</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={cerebrasReasoningFormat}
                  onChange={(e) => setCerebrasReasoningFormat(e.target.value as "parsed" | "raw" | "hidden")}
                >
                  <option value="parsed">parsed（推荐，可显示思考流）</option>
                  <option value="raw">raw（思考拼入正文）</option>
                  <option value="hidden">hidden（隐藏思考）</option>
                </select>
              </label>
              <div className="text-xs text-orange-800 lg:col-span-2">
                推荐使用 <code>medium + parsed</code>。选择 <code>hidden</code> 后模型仍会思考并计费，但仪表盘看不到思考内容；<code>raw</code> 可能干扰翻译 JSON 解析。
              </div>
            </div>
          ) : null}
          <label className="flex items-start gap-2 rounded border border-violet-200 bg-violet-50 p-3 text-sm lg:col-span-2">
            <input className="mt-0.5" type="checkbox" checked={openaiEnableThinking} onChange={(e) => setOpenaiEnableThinking(e.target.checked)} />
            <span>
              <span className="font-medium text-violet-950">启用翻译 Think（流式思考）</span>
              <span className="mt-1 block text-xs text-violet-800">
                {openaiApiType === "cerebras" ? (
                  <>字幕翻译会发送 <code>reasoning_effort</code> 与 <code>reasoning_format</code> 并使用 SSE；选择 parsed 时，思考增量会显示在仪表盘的最近 Agent／对话流。</>
                ) : (
                  <>字幕翻译会发送 <code>enable_thinking: true</code> 并使用 SSE；思考增量会显示在仪表盘的最近 Agent／对话流。仅适用于支持该参数的 OpenAI 兼容上游。</>
                )}
              </span>
            </span>
          </label>
          </>
          )}
        </div>
      </Section>
  );
}
