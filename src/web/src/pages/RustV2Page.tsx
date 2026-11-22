/**
 * Opt-in VideoRoll 2.0 development console. It never routes production v1 jobs
 * to the Rust worker without an explicit separately configured Rust API.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchJson } from "../lib/http";
import { Button, InlineNotice, Metric, PageHeader } from "../components/ui";

const BASE = (import.meta.env.VITE_RUST_V2_API_URL || "/rust-v2/api/v2").replace(/\/+$/, "");

type Stage = "download" | "subtitle" | "render" | "dubbing" | "publish" | "cleanup";
type State = "pending" | "running" | "awaiting" | "unknown" | "failed" | "succeeded" | "canceled";
type Task = {
  id: string;
  status: string;
  source_url: string | null;
  error_message: string | null;
  created_at: string;
};
type WorkItem = {
  id: string;
  stage: Stage;
  state: State;
  attempts: number;
  max_attempts: number;
  lease_until: string | null;
  available_at: string;
  last_error: string | null;
  result: Record<string, unknown> | null;
};
type Workflow = {
  id: string;
  task_id: string;
  current_stage: Stage;
  state: string;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};
type QueueSnapshot = {
  pending: number;
  runnable: number;
  active: number;
  expired_leases: number;
  awaiting_external: number;
  unknown_outcome: number;
  failed: number;
  succeeded: number;
  oldest_pending_seconds: number | null;
  oldest_active_seconds: number | null;
  per_stage: Array<{ stage: string; pending: number; running: number; unknown: number; failed: number; succeeded: number }>;
};
type Source = {
  id: string;
  source_type: string;
  source_id: string;
  display_name: string | null;
  enabled: boolean;
  auto_process: boolean;
  scan_limit: number;
  last_scan_discovered_count: number;
  last_scan_created_count: number;
  last_scan_error: string | null;
};
type SourceScan = { discovered: number; created: number; duplicated: number; pipelines_started: number };

const STAGES: Record<Stage, string> = {
  download: "下载",
  subtitle: "字幕与翻译",
  render: "渲染",
  dubbing: "中文配音",
  publish: "投稿",
  cleanup: "清理",
};
const STATUS: Record<State, string> = {
  pending: "待处理",
  running: "进行中",
  awaiting: "等待外部执行结果",
  succeeded: "完成",
  failed: "失败",
  unknown: "外部结果待核实",
  canceled: "已取消",
};
const badgeStyle: Record<State, string> = {
  pending: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  running: "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
  awaiting: "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  succeeded: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  failed: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
  unknown: "bg-orange-50 text-orange-900 dark:bg-orange-950 dark:text-orange-200",
  canceled: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
};

function api<T>(path: string, init?: RequestInit): Promise<T> {
  return fetchJson<T>(BASE + path, init);
}
function formatTime(value?: string | null): string {
  if (!value) return "未知";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN");
}
function age(seconds?: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return Math.floor(seconds) + " 秒";
  if (seconds < 3600) return Math.floor(seconds / 60) + " 分";
  return (seconds / 3600).toFixed(1) + " 小时";
}

function JobTimeline({ workflowId, onChanged }: { workflowId: string; onChanged: () => void }) {
  const [jobs, setJobs] = useState<WorkItem[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [unknownJob, setUnknownJob] = useState<string | null>(null);
  const [receipt, setReceipt] = useState("");
  const [proof, setProof] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [status, list] = await Promise.all([
        api<Workflow>("/workflows/" + workflowId),
        api<WorkItem[]>("/workflows/" + workflowId + "/jobs"),
      ]);
      setJobs(list);
      setWorkflow(status);
      setProblem(null);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }, [workflowId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 6_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const action = async (verb: "pause" | "resume" | "cancel") => {
    if (verb === "cancel" && !window.confirm("取消当前 V2 工作流？进行中的外部投稿将保留待核实状态。")) return;
    setSaving(true);
    try {
      await api("/workflows/" + workflowId + "/" + verb, { method: "POST" });
      await refresh();
      onChanged();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };
  const resolve = async () => {
    if (!unknownJob || proof.trim().length < 12 || (confirmed && receipt.trim().length < 6)) {
      setProblem("核实说明至少 12 字符。确认已发布时还必须填写平台稿件 ID。");
      return;
    }
    if (!window.confirm("这会更改投稿的最终状态。请确认你已在平台后台核对真实稿件。")) return;
    setSaving(true);
    try {
      await api("/jobs/" + unknownJob + "/resolve-unknown", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accepted: confirmed, external_id: confirmed ? receipt.trim() : null, evidence: proof.trim() }),
      });
      setUnknownJob(null);
      setProof("");
      setReceipt("");
      await refresh();
      onChanged();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span>工作流：{workflowId.slice(0, 12)}</span>
        <span>状态：<strong>{workflow?.state || "未知"}</strong></span>
        <span>当前阶段：{workflow?.current_stage ? STAGES[workflow.current_stage] : "未知"}</span>
        {workflow?.state === "running" && <button type="button" onClick={() => void action("pause")} disabled={saving} className="rounded border px-2 py-1">暂停</button>}
        {workflow?.state === "paused" && <button type="button" onClick={() => void action("resume")} disabled={saving} className="rounded border px-2 py-1">继续</button>}
        {(workflow?.state === "running" || workflow?.state === "paused") && <button type="button" onClick={() => void action("cancel")} disabled={saving} className="rounded border border-red-300 px-2 py-1 text-red-600">取消</button>}
      </div>
      {problem && <InlineNotice tone="danger">{problem}</InlineNotice>}
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
        {jobs.map((job) => (
          <div key={job.id} className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-medium">{STAGES[job.stage] || job.stage}</div>
              <span className={"rounded px-2 py-0.5 text-xs " + badgeStyle[job.state]}>{STATUS[job.state] || job.state}</span>
            </div>
            <div className="mt-1 text-xs text-slate-500">重试次数：{job.attempts} / {job.max_attempts}</div>
            {job.state === "running" && <div className="mt-1 text-xs text-slate-500">租约截止：{formatTime(job.lease_until)}</div>}
            {job.last_error && <p className="mt-2 break-words text-xs text-red-600">{job.last_error}</p>}
            {job.state === "unknown" && job.stage === "publish" && (
              <button type="button" className="mt-2 rounded border border-orange-300 px-2 py-1 text-xs text-orange-700" onClick={() => setUnknownJob(job.id)}>人工核实平台结果</button>
            )}
          </div>
        ))}
      </div>
      {unknownJob && (
        <div className="space-y-3 rounded-lg border border-orange-300 p-3 dark:border-orange-700">
          <div className="text-sm font-medium">B 站 / 外部平台投稿结果核实</div>
          <p className="text-xs text-slate-600">系统不会因超时自动重发。请先在平台创作中心确认实际是否存在稿件，避免重复投稿。</p>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />已确认平台发布成功</label>
          {confirmed && <input className="w-full rounded border px-3 py-2 text-sm dark:bg-slate-900" value={receipt} onChange={(e) => setReceipt(e.target.value)} placeholder="平台稿件 ID（如 BV...）" />}
          <textarea className="w-full rounded border px-3 py-2 text-sm dark:bg-slate-900" value={proof} onChange={(e) => setProof(e.target.value)} placeholder="核对证据与时间（不少于 12 字符）" rows={3} />
          <div className="flex gap-2">
            <button type="button" onClick={() => void resolve()} disabled={saving} className="rounded bg-slate-900 px-3 py-2 text-sm text-white dark:bg-slate-700">记录核实结果</button>
            <button type="button" onClick={() => setUnknownJob(null)} className="rounded border px-3 py-2 text-sm">取消</button>
          </div>
        </div>
      )}
    </div>
  );
}

type NativeSubtitleState = "pending" | "running" | "submitting" | "retry" | "unknown" | "submitted" | "failed";
type NativeSubtitleRecord = {
  publish_job_id: string;
  task_id: string;
  aid: number;
  bvid: string;
  state: NativeSubtitleState;
  expected_parts: number;
  submitted: Record<string, { cid: number; lan: string; location?: string; reconciled?: boolean }>;
  attempts: number;
  next_attempt_at: string;
  inflight_cid: number | null;
  last_error: string | null;
};
const NATIVE_SUBTITLE_STATUS: Record<NativeSubtitleState, string> = {
  pending: "待同步",
  running: "正在准备",
  submitting: "正在提交",
  retry: "稍后重试",
  unknown: "提交结果待核实",
  submitted: "已提交字幕草稿",
  failed: "同步失败",
};

function NativeSubtitlePanel({ connected }: { connected: boolean }) {
  const [rows, setRows] = useState<NativeSubtitleRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [evidence, setEvidence] = useState("");
  const [saving, setSaving] = useState(false);
  const refresh = useCallback(async () => {
    if (!connected) return;
    try {
      const result = await api<NativeSubtitleRecord[]>("/bilibili/subtitles?limit=60");
      setRows(result);
      setIssue(null);
    } catch (error) {
      setIssue(error instanceof Error ? error.message : String(error));
    }
  }, [connected]);
  useEffect(() => {
    void refresh();
    if (!connected) return;
    const timer = window.setInterval(() => { void refresh(); }, 15_000);
    return () => window.clearInterval(timer);
  }, [connected, refresh]);
  const review = async (row: NativeSubtitleRecord) => {
    if (row.inflight_cid == null || evidence.trim().length < 12) {
      setIssue("必须填写至少 12 字符的核实证据，并确认视频分 P 的 CID。");
      return;
    }
    const description = submitted ? "B站后台确实存在该字幕草稿" : "B站后台确认该字幕草稿不存在";
    if (!window.confirm("已经人工核实：" + description + "？系统将据此继续执行，请勿猜测。")) return;
    setSaving(true);
    try {
      await api("/bilibili/subtitles/" + row.publish_job_id + "/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cid: row.inflight_cid,
          submitted,
          evidence: evidence.trim(),
        }),
      });
      setChecking(null);
      setSubmitted(false);
      setEvidence("");
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };
  return (
    <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-semibold">B站原生字幕同步（独立于投稿）</h2>
        <Button onClick={() => { setLoading(true); void refresh().finally(() => setLoading(false)); }} disabled={!connected || loading}>
          {loading ? "刷新中" : "刷新"}
        </Button>
      </div>
      <p className="mb-3 text-xs text-slate-500">
        已发布稿件不会因字幕同步失败而被撤销。遇到结果不明的草稿时，系统不会自动重复投稿字幕。
        核实的是字幕草稿，不是视频稿件是否发布。
      </p>
      {issue && <InlineNotice tone="danger">{issue}</InlineNotice>}
      {!rows.length && <p className="text-sm text-slate-500">没有待同步或历史字幕任务。</p>}
      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.publish_job_id} className="rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-700">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <a href={"https://www.bilibili.com/video/" + encodeURIComponent(row.bvid)} target="_blank" rel="noreferrer"
                className="font-medium text-sky-700 hover:underline dark:text-sky-300">{row.bvid}</a>
              <span className={row.state === "unknown" ? "font-semibold text-orange-700" :
                row.state === "failed" ? "font-semibold text-red-600" : "text-slate-600 dark:text-slate-300"}>
                {NATIVE_SUBTITLE_STATUS[row.state] || row.state}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-slate-500">
              <span>稿件 aid：{row.aid}</span>
              <span>应有分 P：{row.expected_parts}</span>
              <span>已提交分 P：{Object.keys(row.submitted || {}).length}</span>
              <span>执行次数：{row.attempts}</span>
              {row.state === "retry" && <span>下次重试：{formatTime(row.next_attempt_at)}</span>}
            </div>
            {row.last_error && <p className="mt-2 break-words text-red-600">{row.last_error}</p>}
            {row.state === "unknown" && row.inflight_cid != null && (
              <button type="button" className="mt-2 rounded border border-orange-300 px-2 py-1 text-orange-800"
                onClick={() => { setChecking(row.publish_job_id); setEvidence(""); setSubmitted(false); }}>
                人工核实 CID {row.inflight_cid} 字幕草稿
              </button>
            )}
            {checking === row.publish_job_id && (
              <div className="mt-3 space-y-2 rounded border border-orange-200 p-3 dark:border-orange-900">
                <p>请先登录 B站创作中心，检查 aid {row.aid} / cid {row.inflight_cid} 的中文字幕草稿。</p>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={submitted} onChange={(e) => setSubmitted(e.target.checked)} />
                  已确认草稿存在（不勾选代表确认草稿不存在）
                </label>
                <textarea value={evidence} onChange={(e) => setEvidence(e.target.value)}
                  placeholder="请输入核对时间、位置及证据；不少于 12 字符" rows={2}
                  className="w-full rounded border px-3 py-2 dark:bg-slate-900" />
                <div className="flex gap-2">
                  <button type="button" disabled={saving || evidence.trim().length < 12}
                    onClick={() => void review(row)} className="rounded bg-slate-900 px-3 py-2 text-white disabled:opacity-50">
                    保存核实结论
                  </button>
                  <button type="button" onClick={() => setChecking(null)} className="rounded border px-3 py-2">取消</button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

export default function RustV2Page() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [autoTranslate, setAutoTranslate] = useState(true);
  const [autoDub, setAutoDub] = useState(false);
  const [importing, setImporting] = useState(false);
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const [pipelines, setPipelines] = useState<Record<string, string>>({});
  const [sourceScan, setSourceScan] = useState<SourceScan | null>(null);
  const [busySource, setBusySource] = useState<string | null>(null);
  const [submissionError, setSubmissionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [queue, list, sourceList] = await Promise.all([
        api<QueueSnapshot>("/queue"),
        api<Task[]>("/tasks?limit=60"),
        api<Source[]>("/youtube/sources?limit=100"),
      ]);
      setSnapshot(queue);
      setTasks(list);
      setSources(sourceList);
      setConnected(true);
      setError(null);
    } catch (error) {
      setConnected(false);
      setError(error instanceof Error ? error.message : String(error));
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 6_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const importTask = async () => {
    if (!sourceUrl.trim()) return;
    setImporting(true);
    setSubmissionError(null);
    try {
      const result = await api<{ task: Task; workflow: Workflow | null }>("/auto/youtube", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: sourceUrl.trim(),
          source_license: "authorized",
          configuration: {
            subtitle: { translate_enabled: autoTranslate },
            dubbing: { enabled: autoDub },
            auto_publish: false,
            auto_publish_platforms: [],
          },
        }),
      });
      setSourceUrl("");
      setActiveTask(result.task.id);
      if (result.workflow) setPipelines((current) => ({ ...current, [result.task.id]: result.workflow!.id }));
      await refresh();
    } catch (error) {
      setSubmissionError(error instanceof Error ? error.message : String(error));
    } finally {
      setImporting(false);
    }
  };

  const startTask = async (taskId: string) => {
    setSubmissionError(null);
    try {
      const workflow = await api<Workflow>("/workflows/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId, configuration: { auto_publish: false, auto_publish_platforms: [] } }),
      });
      setPipelines((old) => ({ ...old, [taskId]: workflow.id }));
      setActiveTask(taskId);
    } catch (error) {
      setSubmissionError(error instanceof Error ? error.message : String(error));
    }
  };
  const scan = async (id: string) => {
    setBusySource(id);
    setSourceScan(null);
    try {
      const result = await api<SourceScan>("/youtube/sources/" + id + "/scan", { method: "POST" });
      setSourceScan(result);
      await refresh();
    } catch (error) {
      setSubmissionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusySource(null);
    }
  };
  const selected = useMemo(() => tasks.find((t) => t.id === activeTask), [tasks, activeTask]);
  return (
    <div className="space-y-5">
      <PageHeader title="Rust 2.0 开发控制台" description="独立的 V2 PostgreSQL 工作流与 Worker。此页面不是当前生产调度器的替代品。" />
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
        开发预览：投稿默认关闭。未部署隔离的 Rust API 前，不应操作实际生产任务。
        <span className="ml-2 text-xs">连接状态：{connected ? "已连接" : "未连接"}</span>
      </div>
      {error && <InlineNotice tone="danger">Rust API 不可用：{error}。检查测试环境的 V2 反向代理和管理员会话。</InlineNotice>}
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Metric label="待处理" value={snapshot?.pending ?? "—"} />
        <Metric label="就绪" value={snapshot?.runnable ?? "—"} />
        <Metric label="执行中" value={snapshot?.active ?? "—"} />
        <Metric label="租约过期" value={snapshot?.expired_leases ?? "—"} />
        <Metric label="外部待核实" value={snapshot?.unknown_outcome ?? "—"} />
        <Metric label="最长等待" value={age(snapshot?.oldest_pending_seconds)} />
      </div>
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="mb-3 font-semibold">V2 测试任务</h2>
        <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
          <input type="url" className="min-w-0 rounded-lg border px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder="YouTube 单个视频链接（需有授权）" />
          <Button onClick={() => void importTask()} disabled={importing || !connected || !sourceUrl.trim()}>{importing ? "正在创建" : "创建隔离任务"}</Button>
        </div>
        <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-600 dark:text-slate-300">
          <label className="flex items-center gap-1"><input type="checkbox" checked={autoTranslate} onChange={(e) => setAutoTranslate(e.target.checked)} />翻译</label>
          <label className="flex items-center gap-1"><input type="checkbox" checked={autoDub} onChange={(e) => setAutoDub(e.target.checked)} />自动配音（需独立 YouDub）</label>
          <span>自动投稿：关闭</span>
        </div>
        {submissionError && <InlineNotice tone="danger">{submissionError}</InlineNotice>}
      </section>
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="mb-3 font-semibold">任务记录</h2>
        {tasks.length === 0 ? <p className="text-sm text-slate-500">尚无 V2 任务</p> : (
          <div className="space-y-1.5">
            {tasks.map((task) => (
              <div key={task.id} className="flex flex-wrap items-center gap-2 border-b border-slate-100 py-2 text-xs dark:border-slate-800">
                <button type="button" className="min-w-0 flex-1 truncate text-left font-medium text-slate-700 hover:underline dark:text-slate-200" onClick={() => setActiveTask(task.id)} title={task.source_url || undefined}>{task.source_url || task.id}</button>
                <span className="text-slate-500">{formatTime(task.created_at)}</span>
                <span className="rounded bg-slate-100 px-2 py-0.5 dark:bg-slate-800">{task.status}</span>
                <button type="button" className="rounded border px-2 py-1 dark:border-slate-700" onClick={() => setActiveTask(task.id)}>详情</button>
                {task.status === "CREATED" && <button type="button" className="rounded border px-2 py-1 dark:border-slate-700" onClick={() => void startTask(task.id)}>启动</button>}
              </div>
            ))}
          </div>
        )}
        {activeTask && (
          <div className="mt-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
            <div className="mb-2 text-sm font-semibold">当前任务：{selected?.source_url || activeTask}</div>
            {selected?.error_message && <p className="text-xs text-red-600">{selected.error_message}</p>}
            {pipelines[activeTask] ? (
              <JobTimeline workflowId={pipelines[activeTask]} onChanged={() => void refresh()} />
            ) : <WorkflowLookup taskId={activeTask} onSelected={(workflowId) => setPipelines((m) => ({ ...m, [activeTask]: workflowId }))} />}
          </div>
        )}
      </section>
      <NativeSubtitlePanel connected={connected} />
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="mb-3 font-semibold">YouTube 来源扫描</h2>
        {sources.length===0 ? <p className="text-sm text-slate-500">还没有录入频道或播放列表</p> : sources.map((source) => (
          <div key={source.id} className="flex flex-wrap items-center gap-2 border-b border-slate-100 py-2 text-xs dark:border-slate-800">
            <span className="min-w-0 flex-1 truncate">{source.display_name || source.source_id}</span>
            <span className="text-slate-500">{source.enabled ? "启用" : "禁用"}</span>
            <span className="text-slate-500">{source.auto_process ? "自动处理" : "仅记录"}</span>
            <span>最近发现 {source.last_scan_discovered_count}</span>
            <button type="button" disabled={busySource===source.id} onClick={() => void scan(source.id)} className="rounded border px-2 py-1 dark:border-slate-700">扫描</button>
          </div>
        ))}
        {sourceScan && <p className="mt-2 text-xs text-slate-600">发现 {sourceScan.discovered} 条，新建 {sourceScan.created} 条，重复 {sourceScan.duplicated} 条，启动 {sourceScan.pipelines_started} 条。</p>}
      </section>
    </div>
  );
}
function WorkflowLookup({ taskId, onSelected }: { taskId: string; onSelected: (id: string) => void }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  useEffect(() => {
    let live=true;
    void api<Workflow[]>("/tasks/"+taskId+"/workflows")
      .then((value) => { if (live) setWorkflows(value); })
      .catch(() => { if (live) setWorkflows([]); });
    return () => { live=false; };
  }, [taskId]);
  if (!workflows.length) return <p className="text-xs text-slate-500">没有已记录的 V2 工作流，可以从此任务启动新流程。</p>;
  return <div className="flex flex-wrap gap-2">{workflows.map((v) => (
    <button key={v.id} type="button" onClick={() => onSelected(v.id)} className="rounded border px-2 py-1 text-xs dark:border-slate-700">工作流 {v.id.slice(0,8)} · {v.state}</button>
  ))}</div>;
}
