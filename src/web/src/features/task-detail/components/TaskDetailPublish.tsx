import type { TaskDetailController } from "../useTaskDetailController";
import { Link } from "react-router-dom";
import { activeAccountsForPlatform, PublishPlatform, socialPublishBrowserUrl } from "../../../lib/publish";
import { clampUploadProgress } from "../utils";
export function TaskDetailPublish({ controller }: { controller: TaskDetailController }) {
  const {
    taskId,
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
    retryPublishJob
  } = controller;
  return (
<div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">投稿</div>
        <div className="mt-2 text-xs text-slate-500">
          哔哩哔哩使用现有接口发布；抖音、小红书和快手由独立 SAU 无头浏览器服务发布。
        </div>
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
          <div className="text-xs font-semibold text-slate-700">Publish Batches</div>
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
                      <span className="font-mono">{batch.id}</span>
                      <span className="font-semibold">{batch.state}</span>
                      <span className="text-slate-600">目标：{targets || "-"}</span>
                      <span className="text-slate-600">
                        清理：{batch.cleanup_enqueued_at ? "已投递" : batch.state === "succeeded" ? "待补偿投递" : "未满足条件"}
                      </span>
                    </div>
                    {failures ? <div className="mt-1 text-rose-700">失败：{failures}</div> : null}
                  </div>
                );
              })}
            </div>
          ) : null}

          <div className="text-xs font-semibold text-slate-700">Publish Jobs</div>
          {!publishJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {publishJobs && publishJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {publishJobs && publishJobs.length > 0 ? (
            <div className="mt-2 overflow-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">ID</th>
                    <th className="py-2 pr-3">Batch</th>
                    <th className="py-2 pr-3">Platform</th>
                    <th className="py-2 pr-3">Account</th>
                    <th className="py-2 pr-3">State</th>
                    <th className="py-2 pr-3">External</th>
                    <th className="py-2 pr-3">tid</th>
                    <th className="py-2 pr-3">typeid</th>
                    <th className="py-2 pr-3">Error</th>
                    <th className="py-2 pr-3">Started</th>
                    <th className="py-2 pr-3">Finished</th>
                    <th className="py-2 pr-3">Action</th>
                    <th className="py-2 pr-3">Updated</th>
                  </tr>
                </thead>
                  <tbody>
                  {publishJobs.map((j) => {
                    const isActiveBilibiliUpload = (j.platform ?? "bilibili") === "bilibili" && Boolean(j.upload_active);
                    return (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.batch_id?.slice(0, 8) ?? "-"}</td>
                      <td className="py-2 pr-3">{j.platform ?? "bilibili"}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.account_id?.slice(0, 8) ?? "-"}</td>
                      <td className={`py-2 pr-3 ${j.state === "unknown" || j.state === "submitted" ? "text-amber-700" : ""}`}>
                        <div>{j.state}</div>
                        {isActiveBilibiliUpload ? (
                          <div className="mt-1 min-w-32 text-xs text-sky-800">
                            <div className="flex items-center justify-between gap-2">
                              <span>上传中</span>
                              <span className="font-mono">{clampUploadProgress(j.upload_progress)}%</span>
                            </div>
                            <div className="mt-1 h-1.5 overflow-hidden rounded bg-sky-100">
                              <div
                                className="h-full bg-sky-500 transition-[width] duration-300"
                                style={{ width: `${clampUploadProgress(j.upload_progress)}%` }}
                              />
                            </div>
                          </div>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">
                        {j.external_url ? (
                          <a className="underline" href={j.external_url} target="_blank" rel="noreferrer">
                            {j.external_id ?? j.bvid ?? j.external_url}
                          </a>
                        ) : (
                          j.external_id ?? j.bvid ?? "-"
                        )}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.tid ?? "-"}</td>
                      <td
                        className="py-2 pr-3 text-xs text-slate-600"
                        title={
                          j.typeid_mode === "ai_summary" && j.typeid_selected_by !== "ai_summary" && j.typeid_ai_reason
                            ? `AI 分区失败：${j.typeid_ai_reason}`
                            : ""
                        }
                      >
                        {(() => {
                          const mode = j.typeid_mode ?? "-";
                          const by = j.typeid_selected_by ?? "-";
                          if (mode !== "-" && by !== "-" && mode !== by) return `${mode}→${by}`;
                          return by !== "-" ? by : mode;
                        })()}
                      </td>
                      <td className="py-2 pr-3">
                        {j.error_message ? (
                          <div className="max-w-[36rem] truncate text-xs text-rose-700" title={j.error_message}>
                            {j.error_message}
                          </div>
                        ) : (
                          <span className="text-xs text-slate-400">-</span>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{j.started_at ? new Date(j.started_at).toLocaleString() : "-"}</td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{j.finished_at ? new Date(j.finished_at).toLocaleString() : "-"}</td>
                      <td className="py-2 pr-3">
                        {["failed", "unknown"].includes(j.state) ? (
                          <button
                            className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-800"
                            onClick={() => retryPublishJob(j)}
                          >
                            确认后重试
                          </button>
                        ) : "-"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>
  );
}
