import { useState } from "react";
import type { TaskDetailController } from "../useTaskDetailController";
import { Link } from "react-router-dom";
import { activeAccountsForPlatform, PublishPlatform, socialPublishBrowserUrl } from "../../../lib/publish";
import { clampUploadProgress } from "../utils";
import { InlineNotice, TechnicalDetails } from "../../../components/ui";
import { jobStatusLabel, publishPlatformLabel } from "../../../lib/executionPresentation";
import { presentTaskError } from "../../../lib/taskPresentation";
export function TaskDetailPublish({ controller }: { controller: TaskDetailController }) {
  const [appendDubbingJobId, setAppendDubbingJobId] = useState("");
  const {
    taskId,
    dubbingJobs,
    publishJobs,
    publishBatches,
    setError,
    publisherErrors,
    busy,
    setBusy,
    publishMetaText,
    setPublishMetaText,
    publishPlatform,
    setPublishPlatform,
    publishVideoKey,
    setPublishVideoKey,
    publishTwoParts,
    setPublishTwoParts,
    publishCoverKey,
    setPublishCoverKey,
    publishAccountId,
    setPublishAccountId,
    publishSchedule,
    setPublishSchedule,
    socialAccounts,
    publishPlatformSettings,
    coverFile,
    setCoverFile,
    publishTypeidMode,
    setPublishTypeidMode,
    publishTypeid,
    publishEnableReprint,
    biliTypesBusy,
    typeRecommendBusy,
    typeRecommend,
    publishReview,
    reviewBusy,
    youtubeMeta,
    setDidAutoPickCover,
    generatePublishDraft,
    fetchYouTubeMeta,
    publishPlatformEnabled,
    isYouTubeTask,
    rawAssets,
    finalAssets,
    coverAssets,
    biliTypeOptions,
    publishTypeLabel,
    activeBilibiliUpload,
    loadBilibiliTypes,
    recommendBilibiliTypeid,
    applyPublishTypeid,
    applyPublishEnableReprint,
    runPublishReview,
    submitPublish,
    openPublishDesktop,
    uploadCover,
    savePublishMetaFromText,
    formatPublishMeta,
    retryPublishJob,
    appendPublishedP2,
  } = controller;
  return (
<div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">投稿</div>
        <div className="mt-2 text-xs text-slate-500">
          哔哩哔哩使用现有接口发布；抖音、小红书和快手由独立 SAU 无头浏览器服务发布。
        </div>
        {(() => {
          const job = dubbingJobs.find((item) => item.request.auto_p2_source === "video_pipeline");
          if (!job) return null;
          const done = job.status === "succeeded";
          const failed = ["failed", "canceled", "paused"].includes(job.status);
          const stage = job.output.youdub?.stage ?? "等待加载";
          return <div className={`mt-3 rounded border p-3 text-sm ${failed ? "border-rose-200 bg-rose-50 text-rose-800" : "border-sky-200 bg-sky-50 text-sky-900"}`}>
            <strong>{done ? "YouDub 配音完成，正在进入双分 P 投稿" : failed ? "自动 YouDub 配音未完成，尚未投稿" : "自动 YouDub 配音中 · 尚未提交 B站稿件"}</strong>
            <div className="mt-1 text-xs">{job.progress}% · {stage} · 任务 {job.id.slice(0, 8)}</div>
            {job.error_message ? (
              <TechnicalDetails className="mt-2" summary={presentTaskError(job.error_message)?.title ?? "配音异常"}>
                <div className="whitespace-pre-wrap break-words font-mono">{job.error_message}</div>
              </TechnicalDetails>
            ) : null}
            <div className="mt-2 h-2 overflow-hidden rounded bg-sky-100">
              <div className="h-full bg-sky-500" style={{ width: `${Math.min(100, Math.max(0, job.progress))}%` }}/>
            </div>
          </div>;
        })()}
        {activeBilibiliUpload ? (
          <div className="mt-3 rounded border border-sky-200 bg-sky-50 p-3">
            <div className="flex items-center justify-between gap-3 text-sm text-sky-900">
              <span className="font-medium">哔哩哔哩视频上传中</span>
              <span className="font-mono">{clampUploadProgress(activeBilibiliUpload.upload_progress)}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded bg-sky-100">
              <div
                className="h-full bg-sky-500 transition-[width] duration-300"
                style={{ width: `${clampUploadProgress(activeBilibiliUpload.upload_progress)}%` }}
              />
            </div>
          </div>
        ) : null}
        {Object.keys(publisherErrors).length ? (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
            <div className="font-semibold">投稿附加数据部分不可用</div>
            {Object.entries(publisherErrors).map(([slice, message]) => (
              <div key={slice} className="mt-1 break-words">{slice}: {message}</div>
            ))}
          </div>
        ) : null}
        <div className="mt-3 rounded border p-3">
          <div className="mb-2 text-xs text-slate-500">平台策略</div>
          <div className="flex flex-wrap gap-2">
            {([
              ["bilibili", "哔哩哔哩"],
              ["douyin", "抖音"],
              ["xiaohongshu", "小红书"],
              ["kuaishou", "快手"],
            ] as Array<[PublishPlatform, string]>).map(([platform, label]) => {
              const enabled = Boolean(publishPlatformSettings?.[platform]);
              return (
                <button
                  key={platform}
                  type="button"
                  disabled={!enabled}
                  title={enabled ? `选择${label}` : `请先到投稿设置启用${label}`}
                  onClick={() => setPublishPlatform(platform)}
                  className={`rounded border px-3 py-2 text-sm disabled:cursor-not-allowed disabled:opacity-50 ${publishPlatform === platform && enabled ? "border-slate-900 bg-slate-900 text-white" : "hover:bg-slate-50"}`}
                >
                  {label}{enabled ? "" : "（未启用）"}
                </button>
              );
            })}
          </div>
          {publishPlatformSettings && !Object.values(publishPlatformSettings).some(Boolean) ? (
            <div className="mt-2 text-xs text-amber-700">
              当前没有启用任何投稿方式，请先到 <Link className="underline" to="/settings/publish">投稿设置</Link> 勾选启用。
            </div>
          ) : null}
        </div>
        {publishPlatform === "bilibili" ? (
          <label className="mt-3 flex items-start gap-2 rounded border border-slate-200 p-3 text-sm">
            <input type="checkbox" checked={publishTwoParts}
              onChange={(e) => setPublishTwoParts(e.target.checked)} className="mt-0.5" />
            <span>
              <strong>投稿双分 P（P1 中文字幕 + P2 中文配音）</strong>
              <span className="mt-1 block text-xs text-slate-500">
                P1 使用任务已完成的字幕视频，P2 使用同任务一个成功 YouDub 作业的配音成品；两份视频均就绪后才提交一次 B 站稿件。
                未勾选时保持原有单视频投稿行为。
              </span>
            </span>
          </label>
        ) : null}
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">投稿视频（video_key，可选）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={publishVideoKey}
              onChange={(e) => setPublishVideoKey(e.target.value)}
            >
              <option value="">自动选择最新最终视频</option>
              {rawAssets.length ? (
                <optgroup label="原视频">
                  {[...rawAssets].reverse().map((a) => (
                    <option key={a.id} value={a.storage_key}>
                      原视频 · {a.storage_key}
                    </option>
                  ))}
                </optgroup>
              ) : null}
              {finalAssets.length ? (
                <optgroup label="压制完成视频">
                  {[...finalAssets].reverse().map((a) => (
                    <option key={a.id} value={a.storage_key}>
                      最终视频 · {a.storage_key}
                    </option>
                  ))}
                </optgroup>
              ) : null}
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">cover_key（可选）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={publishCoverKey}
              onChange={(e) => {
                setPublishCoverKey(e.target.value);
                setDidAutoPickCover(true);
              }}
            >
              <option value="">不使用封面</option>
              {[...coverAssets].reverse().map((a) => (
                <option key={a.id} value={a.storage_key}>
                  {a.storage_key}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept="image/*" onChange={(e) => setCoverFile(e.target.files?.[0] ?? null)} />
          <button
            disabled={busy || !coverFile}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            onClick={uploadCover}
          >
            上传封面
          </button>
        </div>

        {publishPlatform === "bilibili" ? (
        <>
        <div className="mt-3 rounded border p-3">
          <div className="text-sm font-semibold text-slate-700">哔哩哔哩策略</div>
          <div className="mt-1 text-xs text-slate-500">
            当后端 <span className="font-mono">BILIBILI_PUBLISH_MODE=mock</span> 时仅返回模拟结果；真实投稿需先在投稿设置中保存 Cookies 并测试登录。
          </div>
          <div className="mt-3 text-xs text-slate-500">分区（tid/typeid）</div>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            <label className="block">
              <div className="mb-1 text-xs text-slate-600">typeid_mode</div>
              <select
                className="w-full rounded border px-3 py-2 text-sm"
                value={publishTypeidMode}
                onChange={(e) => setPublishTypeidMode(e.target.value)}
              >
                <option value="ai_summary">AI（根据字幕总结）</option>
                <option value="bilibili_predict">B站预测（标题/文件）</option>
                <option value="meta">手动（使用 meta.typeid）</option>
              </select>
            </label>

            <label className="block">
              <div className="mb-1 text-xs text-slate-600">typeid（手动选择）</div>
              <select
                className="w-full rounded border px-3 py-2 text-sm"
                value={publishTypeid}
                onChange={(e) => applyPublishTypeid(Number(e.target.value))}
                disabled={publishTypeidMode !== "meta"}
              >
                <option value="">{publishTypeidMode !== "meta" ? "(切换为 手动 才可选择)" : "(请选择分区…)"}</option>
                {biliTypeOptions.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label} ({o.id})
                  </option>
                ))}
              </select>
              <div className="mt-2 text-xs text-slate-500">当前：{publishTypeLabel || "—"}</div>
            </label>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              disabled={biliTypesBusy}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
              onClick={loadBilibiliTypes}
            >
              {biliTypesBusy ? "加载中…" : "加载分区列表"}
            </button>
            <button
              disabled={typeRecommendBusy || !taskId}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
              onClick={recommendBilibiliTypeid}
              title="使用字幕阶段生成的 summary 让 AI 推荐分区"
            >
              {typeRecommendBusy ? "分析中…" : "AI 推荐分区"}
            </button>
            {typeRecommend && !typeRecommend.ok ? (
              <div className="text-xs text-rose-700">AI 推荐失败：{typeRecommend.reason || "unknown error"}</div>
            ) : null}
            {typeRecommend && typeRecommend.ok ? (
              <div className="text-xs text-slate-600">
                AI 推荐：{typeRecommend.path || typeRecommend.typeid}（{typeRecommend.typeid}）
              </div>
            ) : null}
          </div>
        </div>

        <div className="mt-3 rounded border p-3">
          <div className="text-xs text-slate-500">转载</div>
          <label className="mt-2 flex items-center gap-2 text-sm">
            <input type="checkbox" checked={publishEnableReprint} onChange={(e) => applyPublishEnableReprint(e.target.checked)} />
            启用转载（开启=copyright=2；关闭=自制）
          </label>
        </div>
        </>
        ) : (
          <div className="mt-3 rounded border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-semibold text-slate-700">SAU 浏览器发布策略</div>
              {socialPublishBrowserUrl(publishPlatform) ? (
                <button
                  type="button"
                  className="rounded border border-indigo-300 px-3 py-1.5 text-xs text-indigo-700 hover:bg-indigo-50"
                  onClick={async () => {
                    const browserUrl = socialPublishBrowserUrl(publishPlatform);
                    if (!browserUrl) return;
                    const job = (publishJobs ?? []).find(
                      (candidate) => candidate.platform === publishPlatform && candidate.state === "submitting",
                    );
                    if (!job?.id) {
                      setError("请先提交抖音投稿任务；自动化窗口只在该投稿任务执行期间开放");
                      return;
                    }
                    try {
                      await openPublishDesktop(browserUrl, job.id);
                    } catch (e: unknown) {
                      setError(e instanceof Error ? e.message : String(e));
                    }
                  }}
                >
                  打开自动化窗口
                </button>
              ) : null}
            </div>
            <div className="mt-1 text-xs text-amber-700">
              submitted 表示已执行提交但尚未取得平台作品 ID；unknown 表示结果不确定，请先到平台后台确认，避免重复投稿。
            </div>
            {publishPlatform === "douyin" ? (
              <div className="mt-1 text-xs text-slate-500">
                可先打开自动化窗口，再点击投稿；窗口会实时显示 worker 中的抖音浏览器上传和发布过程。
              </div>
            ) : null}
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">账号</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={publishAccountId}
                  onChange={(event) => setPublishAccountId(event.target.value)}
                >
                  <option value="">请选择已校验账号</option>
                  {activeAccountsForPlatform(socialAccounts, publishPlatform)
                    .filter((account) => account.check_state === "valid")
                    .map((account) => (
                      <option key={account.id} value={account.id}>{account.name}</option>
                    ))}
                </select>
                <div className="mt-1 text-xs text-slate-500">账号需先在“投稿设置”中导入 storage_state JSON 并校验成功。</div>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">定时发布（可选）</div>
                <input
                  type="datetime-local"
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={publishSchedule}
                  onChange={(event) => setPublishSchedule(event.target.value)}
                />
              </label>
            </div>
          </div>
        )}

        <div className="mt-3 rounded border p-3">
          <div className="text-xs text-slate-500">AI 审核</div>
          {!publishReview ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {publishReview ? (
            <>
              <div className="mt-2 text-sm text-slate-800">
                状态：
                {!publishReview.enabled
                  ? "未启用"
                  : !publishReview.checked
                    ? "未执行"
                    : publishReview.ok
                      ? "通过"
                      : "不通过"}
              </div>
              {publishReview.checked_at ? (
                <div className="mt-1 text-xs text-slate-500">最近审核：{new Date(publishReview.checked_at).toLocaleString()}</div>
              ) : null}
              {publishReview.reason ? (
                <div
                  className={`mt-2 whitespace-pre-wrap break-words text-xs ${publishReview.ok ? "text-emerald-700" : "text-rose-700"}`}
                >
                  {publishReview.reason}
                </div>
              ) : null}
              {publishReview.matched_blocked_words.length > 0 ? (
                <div className="mt-2 text-xs text-rose-700">命中违禁词：{publishReview.matched_blocked_words.join("、")}</div>
              ) : null}
              {publishReview.risk_tags.length > 0 ? (
                <div className="mt-2 text-xs text-slate-500">风险标签：{publishReview.risk_tags.join("、")}</div>
              ) : null}
              {publishReview.subtitle_chars > 0 ? (
                <div className="mt-2 text-xs text-slate-500">本次审核使用字幕字符数：{publishReview.subtitle_chars}</div>
              ) : null}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  disabled={busy || reviewBusy}
                  className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
                  onClick={runPublishReview}
                >
                  {reviewBusy ? "审核中…" : "执行审核"}
                </button>
              </div>
            </>
          ) : null}
        </div>

        <div className="mt-3">
          <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-600">
            <div>meta.json</div>
            <div className="flex items-center gap-2">
              <button
                disabled={busy}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await generatePublishDraft("default");
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                加载默认
              </button>
              <button
                disabled={busy || !isYouTubeTask}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    let metaIn: any;
                    try {
                      metaIn = JSON.parse(publishMetaText);
                    } catch {
                      throw new Error("publish meta is not valid JSON");
                    }
                    if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) throw new Error("publish meta must be a JSON object");
                    const yt = youtubeMeta ?? (await fetchYouTubeMeta());
                    if (!yt) throw new Error("failed to fetch youtube metadata");
                    await generatePublishDraft("source", metaIn);
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                从 YouTube 填充
              </button>
              <button
                disabled={busy}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={formatPublishMeta}
              >
                格式化
              </button>
              <button
                disabled={busy || !taskId || publishPlatform !== "bilibili"}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={savePublishMetaFromText}
              >
                保存
              </button>
            </div>
          </div>
          <textarea
            className="h-64 w-full rounded border p-3 font-mono text-xs"
            value={publishMetaText}
            onChange={(e) => setPublishMetaText(e.target.value)}
          />
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button
            disabled={busy || !publishPlatformEnabled || (publishPlatform !== "bilibili" && !publishAccountId)}
            onClick={() => submitPublish()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            投稿
          </button>
          {publishReview?.enabled && publishReview.ok === false ? (
            <button
              disabled={busy || !publishPlatformEnabled || (publishPlatform !== "bilibili" && !publishAccountId)}
              onClick={() => submitPublish({ skipReview: true })}
              className="rounded border border-amber-300 px-3 py-2 text-sm text-amber-800 hover:bg-amber-50 disabled:opacity-50"
            >
              忽略审核并投稿
            </button>
          ) : null}
          <Link to="/tasks" className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            返回列表
          </Link>
        </div>

        <div className="mt-4">
          <div className="text-xs font-semibold text-slate-700 dark:text-slate-300">发布批次</div>
          {!publishBatches ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {publishBatches && publishBatches.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {publishBatches && publishBatches.length > 0 ? (
            <div className="mt-2 space-y-2">
              {publishBatches.map((batch) => {
                const targets = batch.expected_targets
                  .map((target) => target.key ?? `${target.platform ?? "unknown"}:${target.account_id ?? "default"}`)
                  .join(", ");
                const failures = Object.entries(batch.outcomes)
                  .filter(([, outcome]) => outcome.state === "failed" || outcome.state === "unknown")
                  .map(([key, outcome]) => `${key}: ${outcome.detail ?? outcome.state}`)
                  .join("; ");
                return (
                  <div key={batch.id} className="rounded border border-slate-200 bg-slate-50 p-2 text-xs">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="font-semibold">{jobStatusLabel(batch.state)}</span>
                      <span className="text-slate-600">目标 {batch.expected_targets.length} 个</span>
                      <span className="text-slate-600">
                        清理：{batch.cleanup_enqueued_at ? "已投递" : batch.state === "succeeded" ? "待补偿投递" : "未满足条件"}
                      </span>
                    </div>
                    {failures ? (
                      <TechnicalDetails className="mt-2" summary="批次异常详情">
                        <div className="whitespace-pre-wrap break-words font-mono">{failures}</div>
                        <div className="mt-2 text-slate-500">批次 ID：{batch.id} · 目标：{targets || "—"}</div>
                      </TechnicalDetails>
                    ) : (
                      <TechnicalDetails className="mt-2" summary="批次技术信息">
                        <div>批次 ID：<span className="font-mono">{batch.id}</span></div>
                        <div className="mt-1">目标：{targets || "—"}</div>
                      </TechnicalDetails>
                    )}
                  </div>
                );
              })}
            </div>
          ) : null}

          <div className="text-xs font-semibold text-slate-700 dark:text-slate-300">发布记录</div>
          {!publishJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {publishJobs && publishJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {publishJobs?.some((j) => j.platform === "bilibili" && j.state === "published" && j.aid) ? (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <label htmlFor="append-dub-job">单独追加 P2：选择已完成的中文配音</label>
              <select id="append-dub-job" className="rounded border px-2 py-1"
                value={appendDubbingJobId || dubbingJobs.find((d) => d.status === "succeeded" && Boolean(d.output.video_key))?.id || ""}
                onChange={(event) => setAppendDubbingJobId(event.target.value)}>
                <option value="" disabled>请选择已完成的配音成品</option>
                {dubbingJobs.filter((d) => d.status === "succeeded" && Boolean(d.output.video_key)).map((d) => (
                  <option key={d.id} value={d.id}>{d.id.slice(0, 8)} · {d.output.video_key || "无 P2 视频"}</option>
                ))}
              </select>
              <span className="text-slate-500">仅追加到下方已有稿件，不会重新投稿 P1。</span>
            </div>
          ) : null}
          {publishJobs && publishJobs.length > 0 ? (
            <div className="mt-3 space-y-3">
              {publishJobs.map((job) => {
                const isActiveBilibiliUpload =
                  (job.platform ?? "bilibili") === "bilibili" && Boolean(job.upload_active);
                const failure = presentTaskError(job.error_message);
                const canAppendP2 =
                  job.platform === "bilibili"
                  && job.state === "published"
                  && Boolean(job.aid)
                  && (!job.auto_dub_p2 || job.auto_dub_p2.state === "failed");
                const completedDub = dubbingJobs.find(
                  (item) => item.status === "succeeded" && Boolean(item.output.video_key)
                );
                return (
                  <div key={job.id} className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div>
                        <div className="font-medium text-slate-950 dark:text-white">
                          {publishPlatformLabel(job.platform)}
                        </div>
                        <div className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                          {jobStatusLabel(job.state)}
                        </div>
                      </div>
                      <div className="text-xs text-slate-500">
                        更新于 {new Date(job.updated_at).toLocaleString()}
                      </div>
                    </div>

                    {isActiveBilibiliUpload ? (
                      <div className="mt-3">
                        <div className="flex items-center justify-between gap-2 text-xs text-sky-800 dark:text-sky-300">
                          <span>正在上传视频</span>
                          <span>{clampUploadProgress(job.upload_progress)}%</span>
                        </div>
                        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-sky-100 dark:bg-sky-950">
                          <div
                            className="h-full rounded-full bg-sky-500"
                            style={{ width: String(clampUploadProgress(job.upload_progress)) + "%" }}
                          />
                        </div>
                      </div>
                    ) : null}

                    {job.auto_dub_p2 ? (
                      <div className="mt-3 text-sm">
                        <span className="text-slate-500">中文配音 P2：</span>
                        <span>{jobStatusLabel(job.auto_dub_p2.state ?? "")}</span>
                        {job.auto_dub_p2.error ? (
                          <TechnicalDetails className="mt-2" summary="P2 异常详情">
                            <div className="whitespace-pre-wrap break-words font-mono">{job.auto_dub_p2.error}</div>
                          </TechnicalDetails>
                        ) : null}
                      </div>
                    ) : null}

                    {job.error_message ? (
                      <InlineNotice className="mt-3" title={failure?.title ?? "发布失败"} tone="danger">
                        {failure?.detail ?? "平台发布没有完成。"}
                        <TechnicalDetails className="mt-2">
                          <div className="whitespace-pre-wrap break-words font-mono">{job.error_message}</div>
                        </TechnicalDetails>
                      </InlineNotice>
                    ) : null}

                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      {job.external_url ? (
                        <a
                          className="rounded border border-slate-300 px-2.5 py-1.5 text-xs hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-900"
                          href={job.external_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          打开平台稿件
                        </a>
                      ) : null}
                      {["failed", "unknown"].includes(job.state) ? (
                        <button
                          className="rounded border border-amber-300 px-2.5 py-1.5 text-xs text-amber-800 dark:text-amber-300"
                          onClick={() => retryPublishJob(job)}
                        >
                          确认后重试
                        </button>
                      ) : null}
                      {canAppendP2 ? (
                        <button
                          className="rounded border border-sky-300 px-2.5 py-1.5 text-xs text-sky-800 dark:text-sky-300"
                          disabled={busy || !completedDub}
                          onClick={() => appendPublishedP2(
                            job,
                            appendDubbingJobId || completedDub?.id || ""
                          )}
                        >
                          追加中配 P2
                        </button>
                      ) : null}
                    </div>

                    <TechnicalDetails className="mt-3" summary="发布技术信息">
                      <div className="grid gap-2 sm:grid-cols-2">
                        <div><span className="text-slate-500">发布作业：</span><span className="font-mono">{job.id}</span></div>
                        <div><span className="text-slate-500">批次：</span><span className="font-mono">{job.batch_id ?? "—"}</span></div>
                        <div><span className="text-slate-500">账号：</span><span className="font-mono">{job.account_id ?? "—"}</span></div>
                        <div><span className="text-slate-500">平台稿件 ID：</span>{job.external_id ?? job.bvid ?? "—"}</div>
                        <div><span className="text-slate-500">分区 ID：</span>{job.tid ?? "—"}</div>
                        <div><span className="text-slate-500">分区策略：</span>{job.typeid_mode ?? "—"} → {job.typeid_selected_by ?? "—"}</div>
                        <div><span className="text-slate-500">开始：</span>{job.started_at ? new Date(job.started_at).toLocaleString() : "—"}</div>
                        <div><span className="text-slate-500">完成：</span>{job.finished_at ? new Date(job.finished_at).toLocaleString() : "—"}</div>
                      </div>
                      {job.typeid_ai_reason ? (
                        <div className="mt-2"><span className="text-slate-500">AI 分区诊断：</span>{job.typeid_ai_reason}</div>
                      ) : null}
                    </TechnicalDetails>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
  );
}
