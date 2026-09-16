import type { TaskDetailController } from "../useTaskDetailController";
import { clampUploadProgress, formatDownloadBytes, youtubeDownloadStatusLabel } from "../utils";
import { Link } from "react-router-dom";
export function TaskDetailMedia({ controller }: { controller: TaskDetailController }) {
  const {
    assets,
    videoFile,
    setVideoFile,
    busy,
    youtubeDownloadProgress,
    isYouTubeTask,
    rawAsset,
    metadataAsset,
    finalAssets,
    uploadVideo,
    downloadYouTubeVideo,
    deleteAsset,
    assetDownloadUrl,
    playoutLinks,
    addToPlayout,
  } = controller;
  const playoutLinksByAsset = new Map((playoutLinks ?? []).map((link) => [link.asset_id, link]));
  return (
<>
      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Upload / Raw Video</div>
        <div className="mt-2 text-xs text-slate-500">已上传：{rawAsset ? rawAsset.storage_key : "无"}</div>
        <div className="mt-1 text-xs text-slate-500">metadata：{metadataAsset ? metadataAsset.storage_key : "无"}</div>
        {isYouTubeTask && youtubeDownloadProgress && youtubeDownloadProgress.status !== "idle" ? (
          <div className={`mt-3 rounded border p-3 ${youtubeDownloadProgress.status === "failed" ? "border-rose-200 bg-rose-50" : "border-sky-200 bg-sky-50"}`}>
            <div className="flex items-center justify-between gap-3 text-sm text-slate-900">
              <span className="font-medium">下载视频 {clampUploadProgress(youtubeDownloadProgress.progress)}%</span>
              <span>{youtubeDownloadStatusLabel(youtubeDownloadProgress.status)}</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded bg-white/80">
              <div
                className={`h-full transition-[width] duration-300 ${youtubeDownloadProgress.status === "failed" ? "bg-rose-500" : "bg-sky-500"}`}
                style={{ width: `${clampUploadProgress(youtubeDownloadProgress.progress)}%` }}
              />
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
              <span>{formatDownloadBytes(youtubeDownloadProgress.downloaded_bytes)}{youtubeDownloadProgress.total_bytes ? ` / ${formatDownloadBytes(youtubeDownloadProgress.total_bytes)}` : ""}</span>
              {youtubeDownloadProgress.speed_bytes_per_second ? <span>{formatDownloadBytes(youtubeDownloadProgress.speed_bytes_per_second)}/s</span> : null}
              {youtubeDownloadProgress.eta_seconds != null && youtubeDownloadProgress.active ? <span>剩余约 {youtubeDownloadProgress.eta_seconds}s</span> : null}
              {youtubeDownloadProgress.filename ? <span className="max-w-full truncate">{youtubeDownloadProgress.filename}</span> : null}
            </div>
            {youtubeDownloadProgress.error ? <div className="mt-2 break-words text-xs text-rose-700">{youtubeDownloadProgress.error}</div> : null}
          </div>
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept="video/*" onChange={(e) => setVideoFile(e.target.files?.[0] ?? null)} />
          <button
            disabled={!videoFile || busy}
            onClick={uploadVideo}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            上传
          </button>
          {isYouTubeTask ? (
            <button
              disabled={busy}
              onClick={downloadYouTubeVideo}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              {youtubeDownloadProgress?.active ? `下载中 ${clampUploadProgress(youtubeDownloadProgress.progress)}%` : "从 YouTube 下载"}
            </button>
          ) : null}
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Assets</div>
        {!assets ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {assets ? (
          <div className="mt-2 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Kind</th>
                  <th className="py-2 pr-3">Key</th>
                  <th className="py-2 pr-3">Created</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {assets.map((a) => {
                  const playoutLink = playoutLinksByAsset.get(a.id);
                  return (
                  <tr key={a.id} className="border-t">
                    <td className="py-2 pr-3 text-xs">{a.kind}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{a.storage_key}</td>
                    <td className="py-2 pr-3 text-xs text-slate-600">{new Date(a.created_at).toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={assetDownloadUrl(a.id)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Download
                        </a>
                        {a.kind === "video_final" ? (
                          <>
                            <button
                              type="button"
                              disabled={busy || playoutLinks === null || Boolean(playoutLink)}
                              className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-700 hover:bg-sky-50 disabled:opacity-50"
                              onClick={() => addToPlayout(a.id)}
                            >
                              {playoutLink ? "已加入播控" : playoutLinks === null ? "查询播控状态…" : "加入播控"}
                            </button>
                            {playoutLink ? (
                              <Link className="rounded border px-2 py-1 text-xs hover:bg-slate-50" to="/playout">
                                打开播控中心
                              </Link>
                            ) : null}
                            <button
                              type="button"
                              disabled={busy}
                              className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                              onClick={() => deleteAsset(a.id)}
                            >
                              Delete
                            </button>
                          </>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {finalAssets.length > 0 ? (
          <div className="mt-3 rounded border bg-slate-50 p-3 text-xs text-slate-700">
            <div className="font-semibold text-slate-700">最终视频</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {finalAssets.map((a) => (
                <a
                  key={a.id}
                  className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-800"
                  href={assetDownloadUrl(a.id)}
                  target="_blank"
                  rel="noreferrer"
                >
                  下载：{a.storage_key.split("/").slice(-1)[0]}
                </a>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      </>
  );
}
