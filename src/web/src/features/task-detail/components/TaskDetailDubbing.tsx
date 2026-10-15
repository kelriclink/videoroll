import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  { id: "youdub:voxcpm2", label: "YouDub · VoxCPM2 原生全流程配音（推荐）" },
  { id: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", label: "Qwen3-TTS 0.6B（旧版）" },
  { id: "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", label: "Qwen3-TTS 1.7B（显存占用更高）" },
  { id: "cosyvoice3:base", label: "CosyVoice 3 Base · 英文转中文声音克隆" },
  { id: "cosyvoice3:rl", label: "CosyVoice 3 RL · 科技术语优先测试" },
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
  needs_review: "需要重新对齐口播",
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
  const [nativeWorkerReady, setNativeWorkerReady] = useState(false);
  const [separationReady, setSeparationReady] = useState(false);
  const [instruct, setInstruct] = useState("");
  const [temperature, setTemperature] = useState(0.9);
  const [topP, setTopP] = useState(0.9);
  const [batchSize, setBatchSize] = useState<1 | 2 | 4>(1);
  const [voiceProfiles, setVoiceProfiles] = useState<Array<{ id: string; name: string; duration_seconds: number; reference_text: string; task_id: string }>>([]);
  const [profileId, setProfileId] = useState("");
  const [profileName, setProfileName] = useState("视频原作者");
  const [referenceStart, setReferenceStart] = useState(0);
  const [referenceDuration, setReferenceDuration] = useState(10);
  const [referenceTranscript, setReferenceTranscript] = useState("");
  const [referenceConsent, setReferenceConsent] = useState(false);
  const [suggestedVoices, setSuggestedVoices] = useState<Array<{
    index: number; start_seconds: number; duration_seconds: number;
    reference_text: string; speech_coverage: number; confidence: number | null;
  }>>([]);
  const [suggestedIndex, setSuggestedIndex] = useState(0);
  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [suggestionsError, setSuggestionsError] = useState<string | null>(null);
  const [cosyMode, setCosyMode] = useState<"cross_lingual" | "zero_shot" | "instruct">("cross_lingual");
  const [cosySpeed, setCosySpeed] = useState(1.0);
  const usingCosy = modelPath.startsWith("cosyvoice3:");
  const usingNative = modelPath === "youdub:voxcpm2";
  const [videoKey, setVideoKey] = useState("");
  const [subtitleJobId, setSubtitleJobId] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [autoGroupSpeech, setAutoGroupSpeech] = useState(false);
  const [forceRegenerate, setForceRegenerate] = useState(false);
  const restoredJob = useRef(false);
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
  const eligibleVideos = usingNative ? sourceVideos.filter((item) => item.kind === "video_raw") : sourceVideos;
  const selectedVideo = eligibleVideos.some((item) => item.storage_key === videoKey)
    ? videoKey
    : eligibleVideos[0]?.storage_key ?? "";
  const selectedSubtitle = finishedSubtitles.some((item) => item.id === subtitleJobId)
    ? subtitleJobId
    : finishedSubtitles[0]?.id ?? "";

  const reloadJobs = useCallback(async () => {
    try {
      const found = await tasksApi.dubbingJobs(taskId);
      // Always prefer the newest attempt; never resurrect an older pending
      // review form when a newer manual dubbing version exists.
      const ordered = [...found].sort((a, b) => b.created_at.localeCompare(a.created_at));
      setJobs(ordered);
      if (!restoredJob.current) {
        const editable = ["preview_ready", "needs_review", "paused", "failed"].includes(ordered[0]?.status ?? "")
          ? ordered[0] : undefined;
        if (editable) {
          restoredJob.current = true;
          setModelPath(editable.request.model_path ?? MODELS[0].id);
          setVoice(editable.request.voice ?? "Vivian");
          setVideoKey(editable.request.subtitle_video_key ?? "");
          setSubtitleJobId(editable.subtitle_job_id ?? "");
          const q = editable.request.quality;
          setInstruct(q?.instruct ?? "");setTemperature(q?.temperature ?? 0.9);
          setTopP(q?.top_p ?? 0.9);setCosySpeed(q?.cosy_speed ?? 1.0);
          setCosyMode((q?.cosy_mode as typeof cosyMode) || "cross_lingual");
          setProfileId(q?.profile_id || "");
          setMixMode((q?.mix_mode as typeof mixMode) || "replace");
          setBatchSize((q?.batch_size as 1 | 2 | 4) || 1);
          setAutoGroupSpeech(q?.auto_group_speech ?? false);
        }
      }
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
      models: Array<{ model: string; installed: boolean; runtime_ready?: boolean }> ;
      voice_profiles?: Array<{ id: string; name: string; duration_seconds: number; reference_text: string; task_id: string }>;
      selection: { model: string; voice: string };
      worker: { available: boolean; state: string };
      separation: { available: boolean };
    }>(orchestratorUrl("/settings/dubbing"))
      .then((state) => {
        if (!active) return;
        setInstalledModels(state.models.filter((entry) => entry.installed && entry.runtime_ready !== false).map((entry) => entry.model));
        setVoiceProfiles(state.voice_profiles ?? []);
        const currentTaskProfiles = (state.voice_profiles ?? []).filter((profile) => profile.task_id === taskId);
        setProfileId((current) => currentTaskProfiles.some((profile) => profile.id === current)
          ? current : currentTaskProfiles[currentTaskProfiles.length - 1]?.id ?? "");
        if (!restoredJob.current) {
          setModelPath(state.models.some((m) => m.model === "youdub:voxcpm2" && m.installed && m.runtime_ready !== false) ? "youdub:voxcpm2" : state.selection.model);
          setVoice(state.models.some((m) => m.model === "youdub:voxcpm2" && m.installed) ? "reference" : state.selection.voice);
        }
        setWorkerReady(state.worker?.available ?? false);
        setNativeWorkerReady(state.worker?.state === "active");
        setSeparationReady(state.separation?.available ?? false);
      })
      .catch(() => { if (active) setInstalledModels([]); });
    return () => { active = false; };
  }, [taskId]);

  // On selecting CosyVoice, use the original-language ASR already generated by
  // subtitles. These proposals only contain original timestamps and words;
  // no transcription or manual minute/second input is required.
  useEffect(() => {
    if (!usingCosy || !selectedSubtitle) {
      setSuggestedVoices([]);setSuggestionsError(null);return;
    }
    let active=true;
    setSuggestionsLoading(true);
    setSuggestionsError(null);
    void fetchJson<{ candidates: Array<{ index: number; start_seconds: number; duration_seconds: number;
      reference_text: string; speech_coverage: number; confidence: number | null }> }>(
      orchestratorUrl(`/tasks/${taskId}/dubbing/voice_candidates?subtitle_job_id=${encodeURIComponent(selectedSubtitle)}`),
    ).then((data) => {
      if (!active) return;
      setSuggestedVoices(data.candidates);
      setSuggestedIndex(0);
    }).catch((reason) => {
      if (!active) return;
      setSuggestedVoices([]);
      setSuggestionsError(String(reason));
    }).finally(() => {if (active) setSuggestionsLoading(false);});
    return () => {active=false;};
  }, [usingCosy, selectedSubtitle, taskId]);

  const running = jobs.some((job) => RUNNING.has(job.status));
  const blockedByExisting = jobs.some((job) => BLOCKING.has(job.status) && job.id === jobs[0]?.id);
  const editableJob = jobs[0] && ["preview_ready", "needs_review", "paused", "failed"].includes(jobs[0].status)
    && jobs[0].subtitle_job_id === selectedSubtitle && jobs[0].request.subtitle_video_key === selectedVideo
    ? jobs[0] : undefined;
  const trulyBusy = jobs.some((job) => RUNNING.has(job.status) ||
    ["queued", "pause_requested", "cancel_requested"].includes(job.status));
  const canGenerate = Boolean((usingNative ? nativeWorkerReady : workerReady) && installedModels.includes(modelPath) && selectedVideo && selectedSubtitle && task?.status !== "CANCELED"
    && (!usingCosy || (profileId !== "" && (cosyMode !== "zero_shot" || Boolean(voiceProfiles.find((x) => x.id === profileId)?.reference_text)) && (cosyMode !== "instruct" || Boolean(instruct.trim()))))
    && !trulyBusy && (!blockedByExisting || Boolean(editableJob)) && !submitting
    && (mixMode !== "background_only" || separationReady));

  async function continueWithYouDub(job: DubbingJob) {
    if (submitting) return;
    setSubmitting(true);setError(null);setMessage(null);
    try {
      const q = job.request.quality;
      await tasksApi.rerunDubbing(taskId, job.id, {
        subtitle_job_id: job.subtitle_job_id ?? "",
        subtitle_video_key: job.request.subtitle_video_key ?? "",
        operation_key: `p2:youdub-resume:${Date.now()}`,
        model_path: job.request.model_path ?? modelPath,
        voice: job.request.voice ?? voice,
        retain_original_audio: job.request.retain_original_audio ?? false,
        preview_only: job.request.preview_only ?? false,
        instruct: q?.instruct ?? "", temperature: q?.temperature ?? 0.9,
        top_p: q?.top_p ?? 0.9, batch_size: (q?.batch_size as 1 | 2 | 4) ?? 1,
        mix_mode: (q?.mix_mode as "replace" | "original_low" | "background_only") ?? "replace",
        reference_profile_id: q?.profile_id ?? null,
        cosy_mode: (q?.cosy_mode as "cross_lingual" | "zero_shot" | "instruct") ?? "cross_lingual",
        cosy_speed: q?.cosy_speed ?? 1,
        debug_timing: false,
        auto_group_speech: q?.auto_group_speech ?? false, auto_rewrite_speech: false,
      }, false);
      setMessage("已请求 YouDub 配音引擎继续：保留音频缓存，由 YouDub 全局/局部伸缩并顺延过长语音；不再限制在单句字幕窗口内。");
      await reloadJobs();
    } catch (reason) {setError(String(reason));}
    finally {setSubmitting(false);}
  }

  async function submitJob(request: import("../../../api/tasks").DubbingRequest) {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    setMessage(null);
    try {
      // A new manual version replaces the old editing context. A historical
      // preview must not silently capture a request for the latest version.
      const latest = jobs[0];
      const matching = latest && ["preview_ready", "needs_review", "paused", "failed"].includes(latest.status)
        && latest.subtitle_job_id === request.subtitle_job_id && latest.request.subtitle_video_key === request.subtitle_video_key
        ? latest : undefined;
      const response = matching
        ? await tasksApi.rerunDubbing(taskId, matching.id, request, forceRegenerate)
        : await tasksApi.startDubbing(taskId, request);
      setMessage(`${request.preview_only ? "首句试听" : "完整配音"}任务已提交：${response.job_id}。${request.preview_only ? "可以反复调参试听。" : "正在生成整个视频。"}`);
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
      setMessage("已按试听设置提交完整配音。");
      await reloadJobs();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  async function createAutoReference() {
    if (!referenceConsent || !selectedSubtitle || !suggestedVoices.length || submitting) return;
    setSubmitting(true);setError(null);setMessage(null);
    try {
      const record = await fetchJson<{ id: string; name: string; duration_seconds: number;
        reference_text: string; task_id: string }>(
        orchestratorUrl(`/tasks/${taskId}/dubbing/voice_profiles/auto`), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subtitle_job_id: selectedSubtitle,
            candidate_index: suggestedIndex, authorization_confirmed: true }),
        });
      setVoiceProfiles((before) => [...before, record]);
      setProfileId(record.id);
      setMessage("已按原语言 ASR 自动选取并提取参考声音。先试听声音质量，再点击首句配音试听。");
    } catch (reason) {setError(String(reason));}
    finally {setSubmitting(false);}
  }

  async function createReference() {
    if (!referenceConsent || !selectedVideo || submitting) return;
    setSubmitting(true); setError(null);
    try {
      const profile = await fetchJson<{ id: string; name: string; duration_seconds: number; reference_text: string; task_id: string }>(
        orchestratorUrl("/settings/dubbing/voices"), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_id: taskId, asset_key: selectedVideo, name: profileName,
            start_seconds: referenceStart, duration_seconds: referenceDuration,
            reference_text: referenceTranscript, authorization_confirmed: true }),
        });
      setVoiceProfiles((items) => [...items, profile]);setProfileId(profile.id);
      setMessage("参考音频已提取为 16 kHz WAV。请先试听确认声音清晰，再创建配音任务。");
    } catch (reason) {setError(String(reason));}
    finally {setSubmitting(false);}
  }

  const audioAssets = (assets ?? []).filter((item) => item.kind === "dubbing_audio");
  const videoAssets = (assets ?? []).filter((item) => item.kind === "video_dubbed");

  return (
    <section className="rounded border bg-white p-4" aria-label="中文配音">
      <div className="text-base font-semibold text-slate-900">中文配音 · 统一手动工作台</div>
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
            {eligibleVideos.length === 0 ? <option value="">{usingNative ? "YouDub 必须使用原视频，不能使用已烧录字幕的 P1" : "尚未找到原视频或 P1"}</option> : null}
            {eligibleVideos.map((item) => (
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
            {usingCosy || usingNative ? <option value="reference">视频原声 · YouDub 自动提取参考音频</option> : VOICES.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className="text-sm text-slate-700">
          TTS 模型
          <select className="mt-1 w-full rounded border p-2" value={modelPath}
            onChange={(e) => {setModelPath(e.target.value);setVoice(e.target.value.startsWith("cosyvoice3:") || e.target.value === "youdub:voxcpm2" ? "reference" : "Vivian");if (e.target.value.startsWith("cosyvoice3:") || e.target.value === "youdub:voxcpm2") setBatchSize(1);}} disabled={running || submitting}>
            {MODELS.map((item) => <option value={item.id} key={item.id} disabled={!installedModels.includes(item.id)}>{item.label}{installedModels.includes(item.id) ? "" : " · 未下载"}</option>)}
          </select>
        </label>
      </div>
      {usingNative ? <div className="mt-3 rounded border border-sky-200 bg-sky-50 p-3 text-sm">
        YouDub 原生引擎负责 Demucs、人声参考、VoxCPM2 语音克隆、时间轴对齐及 MP4 合成。
        此模式只生成完整视频，不使用 VideoRoll 的旧逐句试听和加速逻辑。
      </div> : null}
      {usingCosy ? <div className="mt-3 rounded border border-sky-200 bg-sky-50 p-3 space-y-3 text-sm">
        <div className="font-semibold">CosyVoice 3 · 原作者声音克隆（需要拥有授权）</div>
        <div className="grid gap-3 md:grid-cols-2">
          <label>保存的参考声音
            <select className="mt-1 block w-full rounded border p-2" value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              <option value="">请选择已授权的声音</option>
              {voiceProfiles.filter((p) => p.task_id === taskId).map((p) => <option key={p.id} value={p.id}>{p.name} · {p.duration_seconds.toFixed(1)} 秒</option>)}
            </select>
          </label>
          <label>克隆方式
            <select className="mt-1 block w-full rounded border p-2" value={cosyMode} onChange={(e) => setCosyMode(e.target.value as typeof cosyMode)}>
              <option value="cross_lingual">跨语言 · 英语声音说中文（推荐）</option>
              <option value="zero_shot">零样本 · 需要准确的参考转录文本</option>
              <option value="instruct">指令控制 · 需要语气指令</option>
            </select>
          </label>
          <label>语速（0.8–1.25，仅非流式）
            <input type="number" className="mt-1 block w-full rounded border p-2" min={0.8} max={1.25} step={0.05} value={cosySpeed}
              onChange={(e) => setCosySpeed(Number(e.target.value))}/>
          </label>
          {profileId ? <div className="flex items-end"><audio controls preload="none" src={orchestratorUrl(`/settings/dubbing/voices/${profileId}/audio`)} /></div> : null}
        </div>
        <details className="rounded border border-sky-300 bg-white p-3 space-y-2" open={!profileId}>
          <summary className="cursor-pointer font-medium">{profileId ? "更换参考声音（可选）" : "自动提取参考声音"}</summary>
          {suggestionsLoading ? <p>正在从原语言识别片段中寻找合适的声音…</p> : null}
          {suggestionsError ? <p className="text-amber-700">自动选取不可用：{suggestionsError}。可展开下面的高级手动选项。</p> : null}
          {suggestedVoices.length > 0 ? <>
            <label className="block">参考片段
              <select className="mt-1 block w-full rounded border p-2" value={suggestedIndex}
                onChange={(e) => setSuggestedIndex(Number(e.target.value))} disabled={submitting}>
                {suggestedVoices.map((candidate,i) => <option key={candidate.index} value={candidate.index}>
                  {i === 0 ? "推荐 · " : "备选 · "}{candidate.duration_seconds.toFixed(1)} 秒 · 原视频 {candidate.start_seconds.toFixed(1)} 秒处
                </option>)}
              </select>
            </label>
            <div className="rounded bg-slate-50 p-2 text-slate-700">
              <div className="text-xs text-slate-500">字幕自动识别到的原语言台词（仅展示，无需修改）</div>
              {suggestedVoices.find((candidate) => candidate.index === suggestedIndex)?.reference_text}
            </div>
            <label className="flex items-start gap-2">
              <input type="checkbox" checked={referenceConsent} onChange={(e) => setReferenceConsent(e.target.checked)}/>
              我确认拥有使用此声音进行克隆配音的授权
            </label>
            <button type="button" disabled={!referenceConsent || submitting || !selectedSubtitle}
              onClick={() => void createAutoReference()}
              className="rounded bg-sky-800 px-3 py-2 text-white disabled:opacity-50">
              {submitting ? "正在处理…" : "自动提取推荐声音并保存"}
            </button>
            <p className="text-xs text-slate-600">根据原声识别时间轴自动挑选连续人声；不能保证完全无人声重叠或背景音乐，建议试听后确认。</p>
          </> : null}
        </details>
        <details><summary className="cursor-pointer font-medium">高级：手动指定参考声音（自动选取失败时使用）</summary>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            <label>声音名称<input className="mt-1 block w-full rounded border p-2" value={profileName} onChange={(e) => setProfileName(e.target.value)} /></label>
            <label>开始秒数<input type="number" min={0} step={0.5} className="mt-1 block w-full rounded border p-2" value={referenceStart} onChange={(e) => setReferenceStart(Number(e.target.value))}/></label>
            <label>参考秒数（3–30 秒）<input type="number" min={3} max={30} step={1} className="mt-1 block w-full rounded border p-2" value={referenceDuration} onChange={(e) => setReferenceDuration(Number(e.target.value))}/></label>
            <label>原声对应转录文字（零样本必填）<input className="mt-1 block w-full rounded border p-2" value={referenceTranscript} onChange={(e) => setReferenceTranscript(e.target.value)} /></label>
          </div>
          <label className="mt-3 flex items-start gap-2"><input type="checkbox" checked={referenceConsent} onChange={(e) => setReferenceConsent(e.target.checked)}/>我已确认拥有克隆该声音的授权</label>
          <button type="button" disabled={!referenceConsent || submitting || !selectedVideo} onClick={() => void createReference()}
            className="mt-2 rounded bg-sky-800 px-3 py-2 text-white disabled:opacity-50">提取并保存参考音频</button>
        </details>
      </div> : null}
      {!usingNative ? <details className="mt-3 rounded border border-slate-200 p-3">
        <summary className="cursor-pointer text-sm font-medium">高级参数 · 原声混音、批量与语气指令</summary>
        <label className="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={autoGroupSpeech}
            onChange={(event) => setAutoGroupSpeech(event.target.checked)} disabled={trulyBusy || submitting}/>
          实验性合并短字幕（保留原文，默认关闭）
        </label>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <label className="text-sm text-slate-700">原声音轨
          <select className="mt-1 block w-full rounded border p-2" value={mixMode}
            onChange={(e) => setMixMode(e.target.value as typeof mixMode)} disabled={trulyBusy || submitting}>
            <option value="replace">仅中文配音（替换原声）</option>
            <option value="original_low">低音量原声混音（包含外语人声）</option>
            <option value="background_only" disabled={!separationReady}>Demucs 分离背景音（{separationReady ? "可用" : "未安装"}）</option>
          </select>
        </label>
        <label className="text-sm text-slate-700">每批生成句数（V100 建议 1）
          <select className="mt-1 block w-full rounded border p-2" value={batchSize}
            onChange={(e) => setBatchSize(Number(e.target.value) as 1 | 2 | 4)} disabled={trulyBusy || submitting || usingCosy}>
            <option value={1}>1 · 单句稳妥</option><option value={2}>2 · 批量实验</option><option value={4}>4 · 批量实验</option>
          </select>
        </label>
        <label className="text-sm text-slate-700">语气指令（Qwen 1.7B / CosyVoice 指令模式）
          <input className="mt-1 block w-full rounded border p-2" value={instruct} maxLength={120}
            disabled={(modelPath.includes("0.6B") && !usingCosy) || trulyBusy || submitting}
            onChange={(e) => setInstruct(e.target.value)} placeholder="例如：自然清晰，语速稍快，像科普讲解" />
        </label>
        {!usingCosy ? <div className="grid grid-cols-2 gap-2">
          <label className="text-sm text-slate-700">temperature
            <input type="number" min={0.3} max={1.5} step={0.1} className="mt-1 block w-full rounded border p-2"
              value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} />
          </label>
          <label className="text-sm text-slate-700">top_p
            <input type="number" min={0.6} max={1} step={0.05} className="mt-1 block w-full rounded border p-2"
              value={topP} onChange={(e) => setTopP(Number(e.target.value))} />
          </label>
        </div> : null}
      </div>
      </details> : null}
      {!(usingNative ? nativeWorkerReady : workerReady) ? <p className="mt-2 text-sm text-amber-700">配音 Worker 未运行，已禁止创建无执行节点的任务。</p> : null}
      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <p className="text-slate-600">{usingNative ? "完整原生 YouDub：直接运行 VoxCPM2 / Demucs / 对齐 / 合成；禁用旧配音算法。" : "旧版 TTS + YouDub 对齐器（保留供历史兼容）。"}</p>
        {!usingNative ? <label className="flex items-center gap-2">
          <input type="checkbox" checked={forceRegenerate} onChange={(event) => setForceRegenerate(event.target.checked)} disabled={trulyBusy || submitting}/>
          强制重新生成语音（默认关闭；重试建议复用旧缓存）
        </label> : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        {(usingNative ? [false] as const : [false, true] as const).map((preview) => (
          <button key={String(preview)} type="button" className={preview ? "rounded border border-sky-800 px-4 py-2 text-sm text-sky-900 disabled:opacity-50" : "rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"}
            disabled={!canGenerate}
            onClick={() => void submitJob({
              subtitle_job_id: selectedSubtitle,
              subtitle_video_key: selectedVideo,
              operation_key: `p2:debug:${Date.now()}:${preview ? "preview" : "full"}`,
              voice: usingCosy || usingNative ? "reference" : voice,
              model_path: modelPath, retain_original_audio: mixMode === "original_low",
              preview_only: preview, instruct: modelPath.includes("0.6B") ? "" : instruct,
              temperature, top_p: topP, batch_size: usingCosy || usingNative ? 1 : batchSize, mix_mode: mixMode,
              reference_profile_id: usingCosy ? profileId : null,
              cosy_mode: cosyMode, cosy_speed: cosySpeed,
              debug_timing: false,
              auto_group_speech: autoGroupSpeech, auto_rewrite_speech: false,
            })}>
            {submitting ? "正在提交…" : preview ? "生成一句试听" : usingNative ? "使用原生 YouDub 完整配音" : "完整配音"}
          </button>
        ))}
        <button type="button" className="rounded border px-3 py-2 text-sm"
          onClick={() => { void reloadJobs(); void refreshAssets(); }} disabled={submitting}>
          刷新配音进度
        </button>
      </div>
      {!canGenerate && !trulyBusy && !submitting ? (
        <p className="mt-2 text-xs text-amber-700">
          需要已完成的中文字幕任务、仍保留的原视频或 P1，以及已下载的 TTS 模型；已取消的任务不可启动。
        </p>
      ) : null}
      {message ? <p className="mt-2 text-sm text-emerald-700">{message}</p> : null}
      {error ? <p className="mt-2 break-words text-sm text-rose-700">{error}</p> : null}
      <div className="mt-5 border-t pt-3">
        <div className="font-medium">当前配音版本与历史记录</div>
        <p className="mt-1 text-xs text-slate-500">默认操作最新版本；旧试听、取消与待审核记录仅作为历史保留，不会覆盖新配音。</p>
        {loading ? <p className="mt-2 text-sm text-slate-500">正在加载…</p> : null}
        {!loading && jobs.length === 0 ? <p className="mt-2 text-sm text-slate-500">暂无配音任务</p> : null}
        <div className="mt-2 space-y-3">
          {jobs.map((job) => {
            const audio = audioAssets.find((asset) => asset.storage_key === job.output.audio_key);
            const video = videoAssets.find((asset) => asset.storage_key === job.output.video_key);
            return (
              <div key={job.id} className="rounded border border-slate-200 p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{job.id === jobs[0]?.id ? "当前版本 · " : "历史版本 · "}{statusLabel[job.status] ?? job.status}</span>
                  <span className="font-mono text-xs text-slate-500">{job.id.slice(0, 12)}</span>
                </div>
                <div className="mt-2 h-2 rounded bg-slate-100">
                  <div className="h-full rounded bg-sky-600" style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }} />
                </div>
                <div className="mt-1 text-xs text-slate-600">
                  {job.progress}% · {job.request.voice ?? "Vivian"} ·
                  已缓存 {Object.keys(job.output.segments ?? {}).length}/{job.output.segments_total ?? "?"} 段
                </div>
                {job.output.speech_units?.length ? <p className="mt-1 text-xs text-slate-500">
                  配音规划 {job.output.speech_units.length} 单元 · YouDub 按实际音频全局对齐，单句超时不再中止
                </p> : null}
                {job.output.timing_issue ? <p className="mt-2 text-amber-700">
                  旧引擎第 {job.output.timing_issue.segment_index} 段时长限制导致暂停。可继续使用 YouDub 对齐并复用之前生成的音频。
                </p> : null}
                {job.output.performance ? <p className="mt-1 text-xs text-slate-500">已测推理 {job.output.performance.wall_seconds.toFixed(1)} 秒；生成音频 {job.output.performance.audio_seconds.toFixed(1)} 秒；最近批次 {job.output.performance.last_batch_size} 句</p> : null}
                {job.error_message && !job.output.timing_issue ? <div className="mt-2 break-words text-xs text-rose-700">{job.error_message}</div> : null}
                {job.error_message && job.output.timing_issue ? <details className="mt-2 text-xs text-slate-500">
                  <summary className="cursor-pointer">技术错误详情</summary>
                  <pre className="mt-1 whitespace-pre-wrap break-words">{job.error_message}</pre>
                </details> : null}
                <div className="mt-2 flex flex-wrap gap-2">
                  {audio ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, audio.id)}>下载中文音轨 WAV</a> : null}
                  {video ? <a className="rounded border px-2 py-1 text-xs" href={tasksApi.assetDownloadUrl(taskId, video.id)}>下载 P2 MP4</a> : null}
                  {job.status === "succeeded" && (!audio || !video) ? (
                    <button type="button" className="rounded border px-2 py-1 text-xs"
                      onClick={() => void refreshAssets()}>刷新成品资产</button>
                  ) : null}
                  {job.status === "preview_ready" && !job.output.timing_issue && !submitting ? (
                    <button type="button" className="rounded bg-emerald-700 px-3 py-1 text-xs text-white"
                      onClick={() => void confirmPreview(job.id)}>沿用当前试听生成完整配音</button>
                  ) : null}
                  {RUNNING.has(job.status) && !["pause_requested", "cancel_requested"].includes(job.status) && !submitting ? (
                    <button type="button" className="rounded border px-2 py-1 text-xs"
                      onClick={() => void controlJob(job.id, "pause")}>暂停</button>
                  ) : null}
                  {BLOCKING.has(job.status) && job.status !== "cancel_requested" && !submitting ? (
                    <button type="button" className="rounded border border-red-400 px-2 py-1 text-xs text-red-700"
                      onClick={() => void controlJob(job.id, "cancel")}>取消配音</button>
                  ) : null}
                  {job.status === "needs_review" && !submitting ? (
                    <button type="button" className="rounded border border-amber-400 px-2 py-1 text-xs text-amber-800 disabled:opacity-50"
                      onClick={() => void continueWithYouDub(job)}>用 YouDub 引擎继续配音（复用缓存）</button>
                  ) : ["failed", "paused"].includes(job.status) && !submitting ? (
                    <button type="button" className="rounded border border-amber-400 px-2 py-1 text-xs text-amber-800"
                      onClick={() => void retryJob(job.id)}>{job.status === "paused" ? "恢复配音" : "断点重试"}</button>
                  ) : null}
                </div>
                {job.status === "needs_review" ? <p className="mt-2 text-xs text-amber-700">
                  YouDub 按生成语音顺序对齐：单句超时不再中止任务。最终成片可能延长，需试听检查音画偏移。
                </p> : null}
                <TaskDubbingSegments taskId={taskId} job={job} reload={reloadJobs} />
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
