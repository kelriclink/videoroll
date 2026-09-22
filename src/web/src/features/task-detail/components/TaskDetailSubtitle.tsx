import type { TaskDetailController } from "../useTaskDetailController";
import { Link } from "react-router-dom";
import { normalizeYouTubeSubtitleMode, clampText } from "../utils";
export function TaskDetailSubtitle({ controller }: { controller: TaskDetailController }) {
  const {
    task,
    subtitleJobs,
    busy,
    subtitleFormats,
    setSubtitleFormats,
    burnIn,
    setBurnIn,
    softSub,
    setSoftSub,
    videoCodec,
    setVideoCodec,
    useIntelGpu,
    setUseIntelGpu,
    videoPresetText,
    setVideoPresetText,
    videoCrfText,
    setVideoCrfText,
    asrEngine,
    setAsrEngine,
    asrLanguage,
    setAsrLanguage,
    asrModel,
    setAsrModel,
    whisperModels,
    youtubeSubtitleMode,
    setYouTubeSubtitleMode,
    translateEnabled,
    setTranslateEnabled,
    bilingual,
    setBilingual,
    targetLang,
    setTargetLang,
    translateProvider,
    setTranslateProvider,
    translateStyle,
    setTranslateStyle,
    translateEnableSummary,
    setTranslateEnableSummary,
    openaiKeySet,
    isYouTubeTask,
    submitSubtitleJob,
    canResumeSubtitle,
  } = controller;
  return (
<div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Subtitle</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">输出格式</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.srt}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, srt: e.target.checked }))}
                />
                SRT
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.ass}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, ass: e.target.checked }))}
                />
                ASS
              </label>
            </div>
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={burnIn} onChange={(e) => setBurnIn(e.target.checked)} />
                硬字幕（burn-in）
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={softSub} onChange={(e) => setSoftSub(e.target.checked)} />
                软字幕（mkv）
              </label>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_codec（硬字幕输出编码）</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={videoCodec} onChange={(e) => setVideoCodec(e.target.value)}>
                  <option value="av1">av1（体积更小，编码更慢）</option>
                  <option value="h264">h264（兼容更好，编码更快）</option>
                </select>
              </label>
              <div className="mt-2 text-xs text-slate-500">提示：只有在启用 “硬字幕（burn-in）” 时才会用到该编码设置。</div>
            </div>
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={useIntelGpu}
                  onChange={(e) => setUseIntelGpu(e.target.checked)}
                />
                启用 Intel iGPU 硬件编码
              </label>
            </div>
            <div className="mt-2 text-xs text-slate-500">
              提示：这里只加速硬字幕的最终视频编码。字幕烧录本身仍是 CPU 过滤；软字幕只是封装，不走 GPU。当前 Intel 路径支持 h264/av1。
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">
                  {useIntelGpu ? "视频质量（Intel QP/global_quality；兼容字段 video_crf）" : "video_crf（软件编码；可选：留空=默认）"}
                </div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoCrfText}
                  onChange={(e) => setVideoCrfText(e.target.value)}
                  placeholder={
                    useIntelGpu
                      ? videoCodec === "h264"
                        ? "默认 23（Intel h264 CQP）"
                        : "默认 24（Intel av1 global_quality）"
                      : videoCodec === "h264"
                        ? "默认 18（h264 CRF）"
                        : "默认 24（av1 CRF）"
                  }
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">
                {useIntelGpu
                  ? "Intel GPU 下该值按 CQP / global_quality 处理，不是 CRF；越小质量越高。"
                  : "软件编码下该值按 CRF 处理；越小质量越高。"}
              </div>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_preset（可选：留空=默认）</div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoPresetText}
                  onChange={(e) => setVideoPresetText(e.target.value)}
                  placeholder={
                    useIntelGpu
                      ? videoCodec === "h264"
                        ? "留空=驱动默认（Intel h264）"
                        : "留空=驱动默认（Intel av1）；可填 0..13"
                      : videoCodec === "h264"
                        ? "默认 veryfast（h264）"
                        : "默认 4（av1, 0..13 越小越慢）"
                  }
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">
                提示：CPU h264 可用 ultrafast..veryslow；CPU av1（SVT）为 0..13。Intel GPU 开启时，h264 文本 preset 和 av1 的 0..13 都会映射到 VAAPI quality。
              </div>
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">翻译（可选：mock/noop/openai）</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              {isYouTubeTask ? (
                <label className="block min-w-64">
                  <div className="mb-1 text-xs text-slate-600">YouTube 字幕复用</div>
                  <select
                    className="w-full rounded border px-3 py-2 text-sm"
                    value={youtubeSubtitleMode}
                    onChange={(e) => setYouTubeSubtitleMode(normalizeYouTubeSubtitleMode(e.target.value))}
                  >
                    <option value="off">关闭，直接走 ASR</option>
                    <option value="target">优先目标语言字幕</option>
                    <option value="auto_source">优先自动生成原语言字幕</option>
                  </select>
                </label>
              ) : null}
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={translateEnabled} onChange={(e) => setTranslateEnabled(e.target.checked)} />
                启用翻译
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={bilingual} onChange={(e) => setBilingual(e.target.checked)} />
                双语
              </label>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">target_lang</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">provider</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={translateProvider}
                  onChange={(e) => setTranslateProvider(e.target.value)}
                >
                  <option value="mock">mock</option>
                  <option value="noop">noop</option>
                  <option value="openai">openai</option>
                </select>
              </label>

              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">style</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateStyle} onChange={(e) => setTranslateStyle(e.target.value)}>
                  <option value="口语自然">口语自然</option>
                  <option value="正式严谨">正式严谨</option>
                  <option value="电商营销">电商营销</option>
                </select>
              </label>

              <label className="flex items-center gap-2 text-sm md:col-span-2">
                <input
                  type="checkbox"
                  checked={translateEnableSummary}
                  onChange={(e) => setTranslateEnableSummary(e.target.checked)}
                  disabled={translateProvider !== "openai"}
                />
                dynamic summary（仅 openai）
              </label>

              {isYouTubeTask ? (
                <div className="md:col-span-2 text-xs text-slate-500">
                  {youtubeSubtitleMode === "off"
                    ? "逻辑：不复用 YouTube 字幕，直接进入 ASR；若启用翻译，则在 ASR 结果上继续翻译。"
                    : youtubeSubtitleMode === "auto_source"
                      ? "逻辑：优先抓取 YouTube 自动生成的原语言字幕；若启用翻译，则直接进入翻译管线；如果没有可用自动字幕，再回退到 ASR。"
                      : "逻辑：优先找 `target_lang` 对应的 YouTube 字幕；命中后直接复用并跳过翻译；如果没有可用目标字幕，再回退到 ASR。"}
                </div>
              ) : null}

              {translateEnabled && translateProvider === "openai" && openaiKeySet === false ? (
                <div className="md:col-span-2 text-xs text-rose-700">
                  OpenAI API Key 未设置，请先到 <Link className="underline" to="/settings/translate">翻译 / RAG 设置</Link> 保存配置。
                </div>
              ) : null}
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">ASR（语音识别）</div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">engine</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrEngine} onChange={(e) => setAsrEngine(e.target.value)}>
                  <option value="auto">auto（使用后端默认）</option>
                  <option value="mock">mock</option>
                  <option value="faster-whisper">faster-whisper</option>
                  <option value="openvino">openvino（方案2 / Intel Arc）</option>
                  <option value="external-whisper">在线 Whisper（使用 ASR 设置中的服务地址）</option>
                  <option value="groq-whisper">groq-whisper（GroqCloud，自动切片）</option>
                  <option value="cloudflare-workers-ai">cloudflare-workers-ai（原生时间轴）</option>
                </select>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">language</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={asrLanguage} onChange={(e) => setAsrLanguage(e.target.value)} placeholder="auto / zh / en ..." />
              </label>
              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">model（可选：本地模型目录路径）</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={asrModel}
                  onChange={(e) => setAsrModel(e.target.value)}
                >
                  <option value="">(use env default)</option>
                  {asrEngine === "cloudflare-workers-ai" ? (
                    <option value="@cf/openai/whisper-large-v3-turbo">@cf/openai/whisper-large-v3-turbo</option>
                  ) : null}
                  {asrEngine === "groq-whisper" ? (
                    <>
                      <option value="whisper-large-v3-turbo">whisper-large-v3-turbo</option>
                      <option value="whisper-large-v3">whisper-large-v3</option>
                    </>
                  ) : null}
                  {(whisperModels ?? []).map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
                <div className="mt-2 text-xs text-slate-500">
                  提示：`faster-whisper` 和 `openvino` 可以传本地模型目录路径；在线 Whisper 使用“ASR 设置”中保存的服务地址；Groq 使用 `whisper-large-v3(-turbo)`，Cloudflare 使用 `@cf/` 模型 ID。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy || task?.status === "PUBLISHED"}
            onClick={() => submitSubtitleJob({ resume: false })}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            生成字幕
          </button>
          {canResumeSubtitle ? (
            <button
              disabled={busy || task?.status === "PUBLISHED"}
              onClick={() => submitSubtitleJob({ resume: true })}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
              title="沿用上一次失败任务的完整配置继续执行，保留自动投稿等后续流程。"
            >
              从失败处继续
            </button>
          ) : null}
          {task?.status === "PUBLISHED" ? (
            <span className="text-sm text-slate-500">任务已发布；如需重新生成字幕，请创建新任务。</span>
          ) : null}
        </div>

        <div className="mt-4">
          <div className="text-xs font-semibold text-slate-700">Subtitle Jobs</div>
          {!subtitleJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {subtitleJobs && subtitleJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {subtitleJobs && subtitleJobs.length > 0 ? (
            <div className="mt-2 overflow-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">ID</th>
                    <th className="py-2 pr-3">Status</th>
                    <th className="py-2 pr-3">Progress</th>
                    <th className="py-2 pr-3">Error</th>
                    <th className="py-2 pr-3">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {subtitleJobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3">{j.status}</td>
                      <td className="py-2 pr-3">{j.progress}%</td>
                      <td
                        className={`py-2 pr-3 text-xs ${j.error_message ? "max-w-[360px] truncate text-rose-700" : "text-slate-400"}`}
                        title={j.error_message ?? ""}
                      >
                        {j.error_message ? clampText(j.error_message, 160) : "—"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>
  );
}
