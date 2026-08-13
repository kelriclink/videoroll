import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";
import { SourceLicense, SourceType, Task } from "../lib/types";
import { formatYouTubeBatchFailure, parseYouTubeUrlLines, YouTubeBatchFailure } from "./taskNewPage.helpers";

export default function TaskNewPage() {
  const nav = useNavigate();
  const [mode, setMode] = useState<"local" | "youtube" | "youtube-auto">("youtube-auto");
  const [license, setLicense] = useState<SourceLicense>("own");
  const [proofUrl, setProofUrl] = useState<string>("");
  const [youtubeUrl, setYoutubeUrl] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [batchProgress, setBatchProgress] = useState<{ completed: number; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const youtubeUrls = useMemo(() => parseYouTubeUrlLines(youtubeUrl), [youtubeUrl]);

  const canSubmit = useMemo(() => {
    if (busy) return false;
    if (mode === "local") return !!file;
    return youtubeUrls.length > 0;
  }, [busy, mode, file, youtubeUrls]);

  async function createLocalTaskAndUpload() {
    const task = await fetchJson<Task>(orchestratorUrl("/tasks"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_type: "local" satisfies SourceType,
        source_url: null,
        source_license: license,
        source_proof_url: proofUrl.trim() ? proofUrl.trim() : null,
        priority: 0,
        created_by: "web",
      }),
    });

    const fd = new FormData();
    if (!file) throw new Error("no file selected");
    fd.append("file", file, file.name);

    await fetchJson(orchestratorUrl(`/tasks/${task.id}/upload/video`), {
      method: "POST",
      body: fd,
    });

    return task.id;
  }

  async function createYouTubeTask(url: string) {
    const resp = await fetchJson<{ task_id: string }>(orchestratorUrl("/youtube/ingest"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        license,
        proof_url: proofUrl.trim() ? proofUrl.trim() : null,
      }),
    });
    return resp.task_id;
  }

  async function createYouTubeTaskAndAutoRun(url: string) {
    const resp = await fetchJson<{ task_id: string; pipeline_job_id: string }>(orchestratorUrl("/auto/youtube"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        license,
        proof_url: proofUrl.trim() ? proofUrl.trim() : null,
      }),
    });
    return resp.task_id;
  }

  async function createYouTubeTasks() {
    const taskIds: string[] = [];
    const failures: YouTubeBatchFailure[] = [];
    setBatchProgress({ completed: 0, total: youtubeUrls.length });

    for (const [index, url] of youtubeUrls.entries()) {
      try {
        const taskId = mode === "youtube-auto" ? await createYouTubeTaskAndAutoRun(url) : await createYouTubeTask(url);
        taskIds.push(taskId);
      } catch (e: unknown) {
        failures.push({ url, message: e instanceof Error ? e.message : String(e) });
      } finally {
        setBatchProgress({ completed: index + 1, total: youtubeUrls.length });
      }
    }

    if (failures.length > 0) {
      setYoutubeUrl(failures.map((failure) => failure.url).join("\n"));
      setError(formatYouTubeBatchFailure(youtubeUrls.length, taskIds.length, failures));
      return;
    }

    if (taskIds.length === 1) {
      nav(`/tasks/${taskIds[0]}`);
      return;
    }
    nav("/tasks");
  }

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">New Task</div>
        <div className="text-sm text-slate-600">本地上传或 YouTube（白名单/授权）入库</div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex flex-wrap gap-2">
          <button
            className={[
              "rounded border px-3 py-2 text-sm",
              mode === "youtube-auto" ? "border-slate-900 bg-slate-900 text-white" : "bg-white hover:bg-slate-50",
            ].join(" ")}
            onClick={() => setMode("youtube-auto")}
          >
            YouTube 自动模式
          </button>
          <button
            className={[
              "rounded border px-3 py-2 text-sm",
              mode === "youtube" ? "border-slate-900 bg-slate-900 text-white" : "bg-white hover:bg-slate-50",
            ].join(" ")}
            onClick={() => setMode("youtube")}
          >
            YouTube 链接
          </button>
          <button
            className={[
              "rounded border px-3 py-2 text-sm",
              mode === "local" ? "border-slate-900 bg-slate-900 text-white" : "bg-white hover:bg-slate-50",
            ].join(" ")}
            onClick={() => setMode("local")}
          >
            本地上传
          </button>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">授权类型（必填）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={license}
              onChange={(e) => setLicense(e.target.value as SourceLicense)}
            >
              <option value="own">own（自有）</option>
              <option value="authorized">authorized（已授权）</option>
              <option value="cc">cc（可再分发）</option>
              <option value="unknown">unknown（未知，需要补证明）</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">授权证明/协议链接（推荐）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              placeholder="https://..."
              value={proofUrl}
              onChange={(e) => setProofUrl(e.target.value)}
            />
          </label>
        </div>

        {mode === "local" ? (
          <div className="mt-4">
            <label className="block">
              <div className="mb-1 text-xs text-slate-600">选择视频文件</div>
              <input
                type="file"
                accept="video/*"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="block w-full text-sm"
              />
            </label>
          </div>
        ) : (
          <div className="mt-4">
            <label className="block">
              <div className="mb-1 text-xs text-slate-600">YouTube 视频链接（每行一个）</div>
              <textarea
                className="min-h-36 w-full rounded border px-3 py-2 text-sm"
                placeholder={"https://www.youtube.com/watch?v=...\nhttps://youtu.be/...\nhttps://youtube.com/shorts/..."}
                value={youtubeUrl}
                onChange={(e) => setYoutubeUrl(e.target.value)}
              />
            </label>
            <div className="mt-2 text-xs text-slate-500">
              {mode === "youtube-auto"
                ? "自动模式：每个链接会分别创建任务，并按自动模式设置执行下载、字幕/翻译、压制和已勾选渠道的投稿。支持 watch / youtu.be / shorts 链接。"
                : "说明：每个链接会分别创建任务；创建后可在任务详情页下载视频，并自动填充投稿标题、简介和转载来源。"}
            </div>
            {youtubeUrls.length > 0 ? <div className="mt-1 text-xs text-sky-700">已识别 {youtubeUrls.length} 个链接</div> : null}
          </div>
        )}

        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}

        <div className="mt-4 flex items-center gap-2">
          <button
            disabled={!canSubmit}
            onClick={async () => {
              setError(null);
              setBusy(true);
              setBatchProgress(null);
              try {
                if (mode === "local") {
                  const taskId = await createLocalTaskAndUpload();
                  nav(`/tasks/${taskId}`);
                } else {
                  await createYouTubeTasks();
                }
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
                setBatchProgress(null);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy
              ? batchProgress
                ? `处理中 ${batchProgress.completed}/${batchProgress.total}…`
                : "处理中…"
              : mode === "youtube-auto"
                ? `开始自动处理${youtubeUrls.length > 1 ? `（${youtubeUrls.length} 个）` : ""}`
                : mode === "youtube"
                  ? `创建任务${youtubeUrls.length > 1 ? `（${youtubeUrls.length} 个）` : ""}`
                  : "创建任务"}
          </button>
        </div>
      </div>
    </div>
  );
}
