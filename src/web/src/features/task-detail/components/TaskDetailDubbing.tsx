import { useCallback, useEffect, useMemo, useState } from "react";
import { tasksApi } from "../../../api/tasks";
import { TaskDubbingSegments } from "./TaskDubbingSegments";
import type { Asset, DubbingJob, SubtitleJob, Task } from "../../../lib/types";

const VOICES = [
  { id: "Vivian", label: "Vivian · 女声" },
  { id: "Serena", label: "Serena · 女声" },
  { id: "Uncle_Fu", label: "Uncle Fu · 男声" },
  { id: "Dylan", label: "Dylan · 男声" },
  { id: "Eric", label: "Eric · 男声" },
] as const;

const MODELS = [
  { id: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", label: "Qwen3-TTS 0.6B（推荐试用）" },
  { id: "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", label: "Qwen3-TTS 1.7B（显存占用更高）" },
] as const;

const RUNNING = new Set(["queued", "preparing", "synthesizing", "aligning", "mixing"]);

const statusLabel: Record<string, string> = {
  queued: "等待配音 Worker",
  preparing: "准备模型与音频",
  synthesizing: "逐句生成配音",
  aligning: "调整语音时间轴",
  mixing: "合成中文音轨与视频",
  succeeded: "配音完成",
  failed: "配音失败",
  canceled: "已取消",
};

export function TaskDetailDubbing({
  taskId,
  task,
  assets,
  subtitleJobs,
  refreshAssets,
}: {
  taskId: string;
  task: Task | null;
  assets: Asset[] | null;
  subtitleJobs: SubtitleJob[] | null;
  refreshAssets: () => Promise<unknown>;
}) {
  const [jobs, setJobs] = useState<DubbingJob[]>([]);
  const [voice, setVoice] = useState("Vivian");
  const [modelPath, setModelPath] = useState<string>(MODELS[0].id);
  const [mixOriginal, setMixOriginal] = useState(false);
  const [videoKey, setVideoKey] = useState("");
  const [subtitleJobId, setSubtitleJobId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const p1Assets = useMemo(
    () => (assets ?? []).filter((item) => item.kind === "video_final"),
    [assets],
  );
  const finishedSubtitles = useMemo(
    () => (subtitleJobs ?? []).filter((item) => item.status === "succeeded"),
    [subtitleJobs],
  );
  const selectedP1 = p1Assets.some((item) => item.storage_key === videoKey)
    ? videoKey
    : p1Assets[0]?.storage_key ?? "";
  const selectedSubtitle = finishedSubtitles.some((item) => item.id === subtitleJobId)
    ? subtitleJobId
    : finishedSubtitles[0]?.id ?? "";

  const reloadJobs = useCallback(async () => {
    try {
      setJobs(await tasksApi.dubbingJobs(taskId));
      setError(null);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void reloadJobs();
    const interval = window.setInterval(() => { void reloadJobs(); }, 5000);
    return () => window.clearInterval(interval);
  }, [reloadJobs]);

  const running = jobs.some((job) => RUNNING.has(job.status));
  const canGenerate = Boolean(selectedP1 && selectedSubtitle && task?.status !== "CANCELED"
    && task?.status !== "PUBLISHED" && !running && !submitting);

  async function submitJob(request: {
    subtitle_job_id: string;
    subtitle_video_key: string;
    operation_key: string;
    voice: string;
    model_path: string;
    retain_original_audio: boolean;
  }) {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    setMessage(null);
    try {
      const response = await tasksApi.startDubbing(taskId, request);
      setMessage(`配音任务已提交：${response.job_id}`);
      await reloadJobs();
      await refreshAssets();
    } catch (reason) {
      setError(String(reason));
      await reloadJobs();
    } finally {
      setSubmitting(false);
    }
  }

  async function retryJob(jobId: string) {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    setMessage(null);
    try {
      await tasksApi.retryDubbing(taskId, jobId);
      setMessage("已请求恢复任务；服务器仅允许失败或租约过期的配音作业恢复。");
      await reloadJobs();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  const audioAssets = (assets ?? []).filter((item) => item.kind === "dubbing_audio");
  const videoAssets = (assets ?? []).filter((item) => item.kind === "video_dubbed");

  return (
    <section className="rounded border bg-white p-4" aria-label="中文配音">
      <div className="text-base font-semibold text-slate-900">中文配音 · 手动模式</div>
      <p className="mt-1 text-sm text-slate-600">
        复用已经生成的中文翻译与 P1 中文字幕视频。配音结束后提供独立中文音轨和 P2 MP4；
        此操作不会自动投稿，也不会修改 P1。
      </p>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <label className="text-sm text-slate-700">
          中文字幕任务
          <select className="mt-1 w-full rounded border p-2" value={selectedSubtitle}
            onChange={(e) => setSubtitleJobId(e.target.value)} disabled={running || submitting}>
            {finishedSubtitles.length === 0 ? <option value="">无已完成的中文字幕任务</option> : null}
            {finishedSubtitles.map((item) => (
              <option value={item.id} key={item.id}>{item.id.slice(0, 8)} · 已完成</option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          P1 中文字幕视频
          <select className="mt-1 w-full rounded border p-2" value={selectedP1}
            onChange={(e) => setVideoKey(e.target.value)} disabled={running || submitting}>
            {p1Assets.length === 0 ? <option value="">等待 P1 渲染完成</option> : null}
            {p1Assets.map((item) => (
              <option value={item.storage_key} key={item.id}>
                {item.storage_key.split("/").pop()}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          中文音色
          <select className="mt-1 w-full rounded border p-2" value={voice}
            onChange={(e) => setVoice(e.target.value)} disabled={running || submitting}>
            {VOICES.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          TTS 模型
          <select className="mt-1 w-full rounded border p-2" value={modelPath}
            onChange={(e) => setModelPath(e.target.value)} disabled={running || submitting}>
            {MODELS.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </label>
      </div>
      <label className="mt-3 flex items-start gap-2 text-sm text-slate-700">
        <input type="checkbox" checked={mixOriginal} onChange={(e) => setMixOriginal(e.target.checked)}
          disabled={running || submitting} className="mt-0.5" />
        <span>保留低音量原声（会同时保留原视频人声；尚未实现人声分离）</span>
      </label>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="button" className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          disabled={!canGenerate}
          onClick={() => void submitJob({
            subtitle_job_id: selectedSubtitle,
            subtitle_video_key: selectedP1,
            operation_key: `p2:manual:${Date.now()}`,
            voice, model_path: modelPath, retain_original_audio: mixOriginal,
          })}>
          {submitting ? "正在提交…" : running ? "配音任务运行中" : "生成中文配音 P2"}
        </button>
        <button type="button" className="rounded border px-3 py-2 text-sm"
          onClick={() => { void reloadJobs(); void refreshAssets(); }} disabled={submitting}>
          刷新配音进度
        </button>
      </div>
      {!canGenerate && !running && !submitting ? (
        <p className="mt-2 text-xs text-amber-700">
          必须先完成中文字幕任务和 P1 渲染；已发布或已停止的任务不能直接创建新配音。
        </p>
      ) : null}
      {message ? <p className="mt-2 text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="mt-2 break-words text-sm text-rose-700">{error}</p> : null}
      <div className="mt-5 border-t pt-3">
        <div className="font-medium">配音任务记录</div>
        {loading ? <p className="mt-2 text-sm text-slate-500">正在加载…</p> : null}
        {!loading && jobs.length === 0 ? <p className="mt-2 text-sm text-slate-500">暂无配音任务</p> : null}
        <div className="mt-2 space-y-3">
          {jobs.map((job) => {
            const audio = audioAssets.find((asset) => asset.storage_key === job.output.audio_key);
            const video = videoAssets.find((asset) => asset.storage_key === job.output.video_key);
            return (
              <div key={job.id} className="rounded border border-slate-200 p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{statusLabel[job.status] ?? job.status}</span>
                  <span className="font-mono text-xs text-slate-500">{job.id.slice(0, 12)}</span>
                </div>
                <div className="mt-2 h-2 rounded bg-slate-100">
                  <div className="h-full rounded bg-sky-600" style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }} />
                </div>
                <div className="mt-1 text-xs text-slate-600">
                  {job.progress}% · {job.request.voice ?? "Vivian"} ·
                  已缓存 {Object.keys(job.output.segments ?? {}).length}/{job.output.segments_total ?? "?"} 段
                </div>
                {job.error_message ? <div className="mt-2 break-words text-xs text-rose-700">{job.error_message}</div> : null}
                <div className="mt-2 flex flex-wrap gap-2">
                  {audio ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, audio.id)}>下载中文音轨 WAV</a> : null}
                  {video ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, video.id)}>下载 P2 MP4</a> : null}
                  {job.status === "succeeded" && (!audio || !video) ? (
                    <button type="button" className="rounded border px-2 py-1 text-xs"
                      onClick={() => void refreshAssets()}>刷新成品资产</button>
                  ) : null}
                  {job.status !== "succeeded" && job.status !== "canceled" && !submitting ? (
                    <button type="button" className="rounded border border-amber-400 px-2 py-1 text-xs text-amber-800"
                      onClick={() => void retryJob(job.id)}>
                      {job.status === "failed" ? "断点重试" : "尝试恢复卡住任务"}
                    </button>
                  ) : null}
                </div>
                <TaskDubbingSegments taskId={taskId} job={job} reload={reloadJobs} />
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
