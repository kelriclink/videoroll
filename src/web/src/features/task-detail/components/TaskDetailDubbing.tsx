import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../../../lib/http";
import { orchestratorUrl } from "../../../lib/urls";
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

const RUNNING = new Set(["queued", "preparing", "synthesizing", "aligning", "mixing", "pause_requested", "cancel_requested"]);
const BLOCKING = new Set([...RUNNING, "paused", "preview_ready", "needs_review"]);

const statusLabel: Record<string, string> = {
  queued: "等待配音 Worker",
  preparing: "准备模型与音频",
  synthesizing: "逐句生成配音",
  aligning: "调整语音时间轴",
  mixing: "合成中文音轨与视频",
  succeeded: "配音完成",
  failed: "配音失败",
  canceled: "已取消",
  paused: "已暂停",
  pause_requested: "请求暂停，当前句生成结束后生效",
  cancel_requested: "请求取消，当前句生成结束后生效",
  preview_ready: "首句试听已生成，等待确认",
  needs_review: "语音过长，等待修改口播稿",
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
  const [installedModels, setInstalledModels] = useState<string[]>([]);
  const [mixMode, setMixMode] = useState<"replace" | "original_low" | "background_only">("replace");
  const [workerReady, setWorkerReady] = useState(false);
  const [separationReady, setSeparationReady] = useState(false);
  const [instruct, setInstruct] = useState("");
  const [temperature, setTemperature] = useState(0.9);
  const [topP, setTopP] = useState(0.9);
  const [batchSize, setBatchSize] = useState<1 | 2 | 4>(1);
  const [videoKey, setVideoKey] = useState("");
  const [subtitleJobId, setSubtitleJobId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const sourceVideos = useMemo(
    () => (assets ?? []).filter((item) => item.kind === "video_final" || item.kind === "video_raw")
      .sort((a, b) => Number(b.kind === "video_final") - Number(a.kind === "video_final")),
    [assets],
  );
  const finishedSubtitles = useMemo(
    () => (subtitleJobs ?? []).filter((item) => item.status === "succeeded"),
    [subtitleJobs],
  );
  const selectedVideo = sourceVideos.some((item) => item.storage_key === videoKey)
    ? videoKey
    : sourceVideos[0]?.storage_key ?? "";
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

  useEffect(() => {
    let active = true;
    void fetchJson<{
      models: Array<{ model: string; installed: boolean }>;
      selection: { model: string; voice: string };
      worker: { available: boolean; state: string };
      separation: { available: boolean };
    }>(orchestratorUrl("/settings/dubbing"))
      .then((state) => {
        if (!active) return;
        setInstalledModels(state.models.filter((entry) => entry.installed).map((entry) => entry.model));
        setModelPath(state.selection.model);
        setVoice(state.selection.voice);
        setWorkerReady(state.worker?.available ?? false);
        setSeparationReady(state.separation?.available ?? false);
      })
      .catch(() => { if (active) setInstalledModels([]); });
    return () => { active = false; };
  }, [taskId]);

  const running = jobs.some((job) => RUNNING.has(job.status));
  const blockedByExisting = jobs.some((job) => BLOCKING.has(job.status));
  const canGenerate = Boolean(workerReady && installedModels.includes(modelPath) && selectedVideo && selectedSubtitle && task?.status !== "CANCELED"
    && !blockedByExisting && !submitting && (mixMode !== "background_only" || separationReady));

  async function submitJob(request: {
    subtitle_job_id: string;
    subtitle_video_key: string;
    operation_key: string;
    voice: string;
    model_path: string;
    retain_original_audio: boolean;
    preview_only: boolean;
    instruct: string;
    temperature: number;
    top_p: number;
    batch_size: 1 | 2 | 4;
    mix_mode: "replace" | "original_low" | "background_only";
  }) {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    setMessage(null);
    try {
      const response = await tasksApi.startDubbing(taskId, request);
      setMessage(`已提交首句试听任务：${response.job_id}。试听满意后，再点“确认正式生成”。`);
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

  async function controlJob(jobId: string, action: "pause" | "cancel") {
    if (submitting) return;
    setSubmitting(true);
    try {
      await tasksApi.controlDubbing(taskId, jobId, action);
      setMessage(action === "pause" ? "已请求暂停，将在当前推理片段结束后执行。" : "已请求取消，片段文件仍保留。");
      await reloadJobs();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmPreview(jobId: string) {
    setSubmitting(true);
    try {
      await tasksApi.confirmDubbing(taskId, jobId);
      setMessage("已确认首句试听，开始后续配音。");
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
        中文字幕任务完成后即可手动配音，无须等待 P1 渲染。可以选择原视频或已完成的 P1；
        生成独立中文音轨和配音 MP4，后续渲染、分 P 和投稿由你自行操作，不会自动投稿。
      </p>
      <p className="mt-2 text-xs text-slate-600">模型未下载？<Link className="text-sky-700 underline" to="/settings/dubbing">前往中文配音模型管理</Link>，先下载并配置默认推理模型。</p>
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
          配音源视频（原视频 / P1）
          <select className="mt-1 w-full rounded border p-2" value={selectedVideo}
            onChange={(e) => setVideoKey(e.target.value)} disabled={running || submitting}>
            {sourceVideos.length === 0 ? <option value="">尚未找到可用的原视频或 P1</option> : null}
            {sourceVideos.map((item) => (
              <option value={item.storage_key} key={item.id}>
                {item.kind === "video_raw" ? "原视频" : "P1 中文字幕"} · {item.storage_key.split("/").pop()}
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
            {MODELS.map((item) => <option value={item.id} key={item.id} disabled={!installedModels.includes(item.id)}>{item.label}{installedModels.includes(item.id) ? "" : " · 未下载"}</option>)}
          </select>
        </label>
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <label className="text-sm text-slate-700">原声音轨
          <select className="mt-1 block w-full rounded border p-2" value={mixMode}
            onChange={(e) => setMixMode(e.target.value as typeof mixMode)} disabled={blockedByExisting || submitting}>
            <option value="replace">仅中文配音（替换原声）</option>
            <option value="original_low">低音量原声混音（包含外语人声）</option>
            <option value="background_only" disabled={!separationReady}>Demucs 分离背景音（{separationReady ? "可用" : "未安装"}）</option>
          </select>
        </label>
        <label className="text-sm text-slate-700">每批生成句数（V100 建议 1）
          <select className="mt-1 block w-full rounded border p-2" value={batchSize}
            onChange={(e) => setBatchSize(Number(e.target.value) as 1 | 2 | 4)} disabled={blockedByExisting || submitting}>
            <option value={1}>1 · 单句稳妥</option><option value={2}>2 · 批量实验</option><option value={4}>4 · 批量实验</option>
          </select>
        </label>
        <label className="text-sm text-slate-700">语气指令（仅 1.7B 支持）
          <input className="mt-1 block w-full rounded border p-2" value={instruct} maxLength={120}
            disabled={modelPath.includes("0.6B") || blockedByExisting || submitting}
            onChange={(e) => setInstruct(e.target.value)} placeholder="例如：自然清晰，语速稍快，像科普讲解" />
        </label>
        <div className="grid grid-cols-2 gap-2">
          <label className="text-sm text-slate-700">temperature
            <input type="number" min={0.3} max={1.5} step={0.1} className="mt-1 block w-full rounded border p-2"
              value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} />
          </label>
          <label className="text-sm text-slate-700">top_p
            <input type="number" min={0.6} max={1} step={0.05} className="mt-1 block w-full rounded border p-2"
              value={topP} onChange={(e) => setTopP(Number(e.target.value))} />
          </label>
        </div>
      </div>
      {!workerReady ? <p className="mt-2 text-sm text-amber-700">配音 Worker 未运行，已禁止创建无执行节点的任务。</p> : null}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="button" className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          disabled={!canGenerate}
          onClick={() => void submitJob({
            subtitle_job_id: selectedSubtitle,
            subtitle_video_key: selectedVideo,
            operation_key: `p2:manual:${Date.now()}`,
            voice, model_path: modelPath, retain_original_audio: mixMode === "original_low",
            preview_only: true, instruct: modelPath.includes("0.6B") ? "" : instruct,
            temperature, top_p: topP, batch_size: batchSize, mix_mode: mixMode,
          })}>
          {submitting ? "正在提交…" : running ? "配音任务运行中" : "先生成首句试听"}
        </button>
        <button type="button" className="rounded border px-3 py-2 text-sm"
          onClick={() => { void reloadJobs(); void refreshAssets(); }} disabled={submitting}>
          刷新配音进度
        </button>
      </div>
      {!canGenerate && !blockedByExisting && !submitting ? (
        <p className="mt-2 text-xs text-amber-700">
          需要已完成的中文字幕任务、仍保留的原视频或 P1，以及已下载的 TTS 模型；已取消的任务不可启动。
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
                {job.output.timing_issue ? <p className="mt-2 text-amber-700">第 {job.output.timing_issue.segment_index} 句超时：需要 {job.output.timing_issue.required_speed.toFixed(2)}× 语速。请编辑口播文案，再重试。</p> : null}
                {job.output.performance ? <p className="mt-1 text-xs text-slate-500">已测推理 {job.output.performance.wall_seconds.toFixed(1)} 秒；生成音频 {job.output.performance.audio_seconds.toFixed(1)} 秒；最近批次 {job.output.performance.last_batch_size} 句</p> : null}
                {job.error_message ? <div className="mt-2 break-words text-xs text-rose-700">{job.error_message}</div> : null}
                <div className="mt-2 flex flex-wrap gap-2">
                  {audio ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, audio.id)}>下载中文音轨 WAV</a> : null}
                  {video ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, video.id)}>下载 P2 MP4</a> : null}
                  {job.status === "succeeded" && (!audio || !video) ? (
                    <button type="button" className="rounded border px-2 py-1 text-xs"
                      onClick={() => void refreshAssets()}>刷新成品资产</button>
                  ) : null}
                  {job.status === "preview_ready" && !submitting ? (
                    <button type="button" className="rounded bg-emerald-700 px-3 py-1 text-xs text-white"
                      onClick={() => void confirmPreview(job.id)}>确认试听，正式生成剩余片段</button>
                  ) : null}
                  {RUNNING.has(job.status) && !["pause_requested", "cancel_requested"].includes(job.status) && !submitting ? (
                    <button type="button" className="rounded border px-2 py-1 text-xs"
                      onClick={() => void controlJob(job.id, "pause")}>暂停</button>
                  ) : null}
                  {BLOCKING.has(job.status) && job.status !== "cancel_requested" && !submitting ? (
                    <button type="button" className="rounded border border-red-400 px-2 py-1 text-xs text-red-700"
                      onClick={() => void controlJob(job.id, "cancel")}>取消配音</button>
                  ) : null}
                  {["failed", "paused", "needs_review"].includes(job.status) && !submitting ? (
                    <button type="button" className="rounded border border-amber-400 px-2 py-1 text-xs text-amber-800"
                      onClick={() => void retryJob(job.id)}>
                      {job.status === "needs_review" ? "改写后继续生成" : job.status === "paused" ? "恢复配音" : "断点重试"}
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
