import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL, YOUTUBE_INGEST_URL } from "../lib/urls";
import { SourceLicense, SourceType, Task } from "../lib/types";

export default function TaskNewPage() {
  const nav = useNavigate();
  const [mode, setMode] = useState<"local" | "youtube" | "youtube-auto">("local");
  const [license, setLicense] = useState<SourceLicense>("own");
  const [proofUrl, setProofUrl] = useState<string>("");
  const [youtubeUrl, setYoutubeUrl] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = useMemo(() => {
    if (busy) return false;
    if (mode === "local") return !!file;
    return youtubeUrl.trim().length > 0;
  }, [busy, mode, file, youtubeUrl]);

  async function createLocalTaskAndUpload() {
    const task = await fetchJson<Task>(`${ORCHESTRATOR_URL}/tasks`, {
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

    await fetchJson(`${ORCHESTRATOR_URL}/tasks/${task.id}/upload/video`, {
      method: "POST",
      body: fd,
    });

    return task.id;
  }

  async function createYouTubeTask() {
    const resp = await fetchJson<{ task_id: string }>(`${YOUTUBE_INGEST_URL}/youtube/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: youtubeUrl.trim(),
        license,
        proof_url: proofUrl.trim() ? proofUrl.trim() : null,
      }),
    });
    return resp.task_id;
  }

  async function createYouTubeTaskAndAutoRun() {
    const resp = await fetchJson<{ task_id: string; pipeline_job_id: string }>(`${ORCHESTRATOR_URL}/auto/youtube`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: youtubeUrl.trim(),
        license,
        proof_url: proofUrl.trim() ? proofUrl.trim() : null,
      }),
    });
    return resp.task_id;
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
              mode === "local" ? "border-slate-900 bg-slate-900 text-white" : "bg-white hover:bg-slate-50",
            ].join(" ")}
            onClick={() => setMode("local")}
          >
            本地上传
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
              mode === "youtube-auto" ? "border-slate-900 bg-slate-900 text-white" : "bg-white hover:bg-slate-50",
            ].join(" ")}
            onClick={() => setMode("youtube-auto")}
          >
            YouTube 自动模式
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
              <div className="mb-1 text-xs text-slate-600">YouTube 视频链接</div>
            <input
                className="w-full rounded border px-3 py-2 text-sm"
                placeholder="https://www.youtube.com/watch?v=..."
                value={youtubeUrl}
                onChange={(e) => setYoutubeUrl(e.target.value)}
              />
            </label>
            <div className="mt-2 text-xs text-slate-500">
              {mode === "youtube-auto"
                ? "自动模式：会按 Settings 中的默认参数执行（下载→字幕/翻译→硬字幕 burn-in→自动投稿 Bilibili）。"
                : "说明：创建后可在任务详情页一键下载视频，并自动填充投稿标题/简介/转载来源（请确保你拥有版权/已获授权/可再分发）。"}
            </div>
          </div>
        )}

        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}

        <div className="mt-4 flex items-center gap-2">
          <button
            disabled={!canSubmit}
            onClick={async () => {
              setError(null);
              setBusy(true);
              try {
                const taskId =
                  mode === "local"
                    ? await createLocalTaskAndUpload()
                    : mode === "youtube-auto"
                      ? await createYouTubeTaskAndAutoRun()
                      : await createYouTubeTask();
                nav(`/tasks/${taskId}`);
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "处理中…" : mode === "youtube-auto" ? "开始自动处理" : "创建任务"}
          </button>
        </div>
      </div>
    </div>
  );
}
