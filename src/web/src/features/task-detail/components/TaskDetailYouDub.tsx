import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../../../lib/http";
import { orchestratorUrl } from "../../../lib/urls";
import { tasksApi } from "../../../api/tasks";
import type { Asset, DubbingJob, SubtitleJob, Task } from "../../../lib/types";

type NativeControl = {
  installed: boolean; enabled: boolean; accepting_jobs: boolean;
  active_jobs: number; busy: boolean; message: string;
};
type EngineStatus = { native: NativeControl; worker: { state: string } };
const ACTIVE = new Set(["queued", "preparing", "synthesizing", "aligning",
                        "mixing", "pause_requested", "cancel_requested"]);
const LABELS: Record<string, string> = {
  queued: "排队中", preparing: "正在准备 / 分离原声",
  synthesizing: "VoxCPM2 正在生成语音", aligning: "YouDub 正在对齐声音",
  mixing: "YouDub 正在混音 / 合成视频", succeeded: "已完成",
  failed: "失败（可断点恢复）", canceled: "已取消", paused: "已暂停",
  pause_requested: "正在暂停", cancel_requested: "正在取消",
  preview_ready: "旧版试听记录（只读）", needs_review: "旧版待审核记录（只读）",
};
const STAGE_LABELS: Record<string, string> = {
  download: "读取视频", separate: "Demucs 分离声音", asr: "检查字幕",
  asr_fix: "校正原文", translate: "加载中文翻译",
  split_audio: "参考声音切分", tts: "VoxCPM2 语音合成",
  merge_audio: "原生音频对齐 / 混音", merge_video: "MP4 视频合成",
};

export function TaskDetailDubbing({
  taskId, task, assets, subtitleJobs, refreshAssets,
}: {
  taskId: string; task: Task | null; assets: Asset[] | null;
  subtitleJobs: SubtitleJob[] | null; refreshAssets: () => Promise<unknown>;
}) {
  const [jobs, setJobs] = useState<DubbingJob[]>([]);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [subtitleId, setSubtitleId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [historyExpanded, setHistoryExpanded] = useState(false);

  const subtitles = useMemo(
    () => (subtitleJobs ?? []).filter((job) => job.status === "succeeded"),
    [subtitleJobs],
  );
  const rawVideos = useMemo(
    () => (assets ?? []).filter((asset) => asset.kind === "video_raw"),
    [assets],
  );
  const selectedSubtitle = subtitles.find((job) => job.id === subtitleId)?.id ??
    subtitles[0]?.id ?? "";
  const latest = jobs[0];
  const active = jobs.some((job) => ACTIVE.has(job.status));
  const ready = status?.native.accepting_jobs && status.worker.state === "active";
  const canSubmit = Boolean(ready && selectedSubtitle && rawVideos.length === 1 &&
    !active && !busy && task?.status !== "CANCELED");

  const refresh = useCallback(async () => {
    try {
      const [found, engine] = await Promise.all([
        tasksApi.dubbingJobs(taskId),
        fetchJson<EngineStatus>(orchestratorUrl("/settings/dubbing")),
      ]);
      setJobs([...found].sort((a, b) => b.created_at.localeCompare(a.created_at)));
      setStatus(engine);
    } catch (reason) {
      setError(String(reason));
    }
  }, [taskId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function start() {
    if (!canSubmit) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const job = await tasksApi.startDubbing(taskId, {
        subtitle_job_id: selectedSubtitle,
        subtitle_video_key: rawVideos[0].storage_key,
        operation_key: `youdub-native:${crypto.randomUUID()}`,
        model_path: "youdub:voxcpm2",
        voice: "reference", retain_original_audio: false,
        preview_only: false,
      });
      setNotice(`YouDub 原生任务已提交：${job.job_id.slice(0, 8)}，会自动执行完整视频。`);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally { setBusy(false); }
  }

  async function retry(job: DubbingJob) {
    setBusy(true); setError(null); setNotice(null);
    try {
      await tasksApi.retryDubbing(taskId, job.id);
      setNotice("已提交原生 YouDub 断点恢复，保留此前成功的阶段输出。");
      await refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function control(job: DubbingJob, action: "pause" | "cancel") {
    setBusy(true); setError(null);
    try {
      await tasksApi.controlDubbing(taskId, job.id, action);
      setNotice(action === "pause" ? "已请求安全暂停。" : "已请求取消。");
      await refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  const drawJob = (job: DubbingJob) => {
    const native = job.request.model_path === "youdub:voxcpm2";
    const audio = (assets ?? []).find((asset) => asset.storage_key === job.output.audio_key);
    const video = (assets ?? []).find((asset) => asset.storage_key === job.output.video_key);
    return <div key={job.id} className="rounded border p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <strong>{native ? "YouDub · VoxCPM2" : "旧版配音记录（不可再次执行）"} · {LABELS[job.status] ?? job.status}</strong>
        <span className="font-mono text-xs text-slate-500">{job.id.slice(0, 8)}</span>
      </div>
      <div className="h-2 overflow-hidden rounded bg-slate-200">
        <div className="h-full rounded bg-sky-700" style={{ width: `${Math.min(100, Math.max(0, job.progress))}%` }}/>
      </div>
      <p className="text-xs text-slate-600">进度 {job.progress}%{job.output.duration ? ` · 视频 ${job.output.duration.toFixed(2)} 秒` : ""}</p>
      {native && job.output.youdub?.stages?.length ? <div className="flex flex-wrap gap-2 text-xs">
        {job.output.youdub.stages.map((stage) => <span key={stage.name}
          className="rounded border px-2 py-1">
          {STAGE_LABELS[stage.name] ?? stage.name}：{stage.status === "succeeded" ? "完成" : stage.status === "running" ? "执行中" : stage.status === "failed" ? "失败" : "等待"}
          {stage.status === "running" && typeof stage.progress === "number" ? ` ${stage.progress}%` : ""}
        </span>)}
      </div> : null}
      {job.error_message ? <p className="break-all text-xs text-rose-700">{job.error_message}</p> : null}
      <div className="flex flex-wrap gap-2">
        {video ? <a className="rounded border px-3 py-2 text-sm" href={tasksApi.assetDownloadUrl(taskId, video.id)}>下载配音 MP4</a> : null}
        {audio ? <a className="rounded border px-3 py-2 text-sm" href={tasksApi.assetDownloadUrl(taskId, audio.id)}>下载中文 WAV</a> : null}
        {native && (job.status === "failed" || job.status === "paused") && !busy ?
          <button className="rounded border px-3 py-2 text-sm" onClick={() => void retry(job)}>断点继续</button> : null}
        {native && ACTIVE.has(job.status) && !["pause_requested","cancel_requested"].includes(job.status) && !busy ? <>
          <button className="rounded border px-3 py-2 text-sm" onClick={() => void control(job, "pause")}>暂停</button>
          <button className="rounded border border-rose-600 px-3 py-2 text-sm text-rose-700" onClick={() => void control(job, "cancel")}>取消</button>
        </> : null}
      </div>
    </div>;
  };

  return <section className="space-y-4 rounded border bg-white p-4 text-slate-900">
    <div className="flex items-center justify-between gap-3">
      <div>
        <h3 className="font-semibold text-lg">YouDub · 原生中文配音</h3>
        <p className="text-sm text-slate-600">VoxCPM2 + Demucs + YouDub 原生全局对齐、混音与 MP4。已删除旧逐句 TTS 操作。</p>
      </div>
      <Link to="/settings/dubbing" className="text-sm text-sky-700 underline">引擎管理</Link>
    </div>
    <p className="text-sm">模型：{status?.native.installed ? "VoxCPM2 已安装" : "未就绪"} ·
      控制：{status?.native.enabled ? "已启用" : "未启用"} ·
      Worker：{status?.worker.state === "active" ? "运行中" : "未运行"}</p>
    <div className="grid gap-3 md:grid-cols-2">
      <label className="text-sm">中文稿
        <select value={selectedSubtitle} onChange={(event) => setSubtitleId(event.target.value)}
          disabled={busy || active} className="mt-1 w-full rounded border p-2">
          {subtitles.map((item) => <option key={item.id} value={item.id}>{item.id.slice(0, 8)} · 已完成</option>)}
        </select>
      </label>
      <div className="text-sm">原视频
        <div className="mt-1 rounded border p-2 text-slate-600">
          {rawVideos.length === 1 ? rawVideos[0].storage_key.split("/").pop() :
            rawVideos.length === 0 ? "未找到原视频（不能用已烧录字幕的 P1）" : "发现多个原视频，请检查素材"}
        </div>
      </div>
    </div>
    <button type="button" disabled={!canSubmit} onClick={() => void start()}
      className="rounded bg-sky-800 px-4 py-2 text-sm text-white disabled:opacity-50">
      {busy ? "正在提交…" : "开始完整 YouDub 配音"}
    </button>
    <button type="button" onClick={() => { void refresh(); void refreshAssets(); }}
      disabled={busy} className="ml-2 rounded border px-4 py-2 text-sm">刷新状态</button>
    {!ready && <p className="text-sm text-amber-700">先在「引擎管理」中启用 YouDub，并确保配音任务 Worker 正常运行。</p>}
    {error && <p role="alert" className="text-sm text-rose-700 break-all">{error}</p>}
    {notice && <p className="text-sm text-emerald-700">{notice}</p>}
    {latest && <div className="space-y-2">
      <h4 className="font-semibold">当前配音任务</h4>
      {drawJob(latest)}
    </div>}
    {jobs.length > 1 && <div>
      <button type="button" className="rounded border px-3 py-2 text-sm"
        onClick={() => setHistoryExpanded((value) => !value)}>
        {historyExpanded ? "收起历史记录" : `查看历史配音（${jobs.length - 1}）`}
      </button>
      {historyExpanded && <div className="mt-3 space-y-3">{jobs.slice(1).map(drawJob)}</div>}
    </div>}
  </section>;
}
