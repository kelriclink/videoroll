import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import StatusBadge from "../components/StatusBadge";
import { useConfirm, useToast } from "../components/feedbackContext";
import { Button } from "../components/ui";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";
import { Asset, PublishBatch, PublishJob, SubtitleJob, Task } from "../lib/types";
import { activeAccountsForPlatform, PublishPlatformSettings, SocialAccount } from "./settingsPublishPage.helpers";
import { buildPublishActionPayload, createTaskDetailPollPlan, PublishPlatform, socialPublishBrowserUrl } from "./taskDetailPage.helpers";

type SubtitleActionResponse = { job_id: string; status: string };
type PublishResponse = { state: string; platform?: string | null; aid?: string | null; bvid?: string | null; external_id?: string | null; external_url?: string | null; response?: any };
type PublishMetaDraftResponse = { meta: any };
type PublishMetaStoreResponse = { stored: boolean; key: string; meta?: any };
type PublishPlatformSettingsResponse = { platforms: PublishPlatformSettings };
type YouTubeMeta = {
  title: string;
  description: string;
  webpage_url: string;
  uploader?: string | null;
  upload_date?: string | null;
  duration?: number | null;
};
type YouTubeSubtitleMode = "off" | "target" | "auto_source";
type YouTubeMetaActionResponse = { metadata: YouTubeMeta };
type YouTubeDownloadActionResponse = { metadata: YouTubeMeta; video_asset: Asset; metadata_asset: Asset; cover_asset?: Asset | null };
type SubtitleAutoProfile = {
  formats: string[];
  burn_in: boolean;
  soft_sub: boolean;
  ass_style: string;
  video_codec: string;
  use_intel_gpu: boolean;
  video_preset?: string | null;
  video_crf?: number | null;
  asr_engine: string;
  asr_language: string;
  asr_model?: string | null;
  prefer_youtube_subtitles: boolean;
  youtube_subtitle_mode?: YouTubeSubtitleMode | null;
  translate_enabled: boolean;
  translate_provider: string;
  target_lang: string;
  translate_style: string;
  translate_enable_summary: boolean;
  bilingual: boolean;
  auto_publish: boolean;
  publish_typeid_mode?: string | null;
  publish_title_prefix: string;
  publish_translate_title: boolean;
  publish_use_youtube_cover: boolean;
  publish_enable_reprint: boolean;
};

type BiliTypeNode = { id: number; name: string; children?: BiliTypeNode[] };
type BilibiliArchiveTypesResponse = { typelist: BiliTypeNode[] };
type BilibiliTypeRecommendResponse = { ok: boolean; typeid?: number | null; path?: string | null; reason?: string; used_text?: string };
type PublishReview = {
  enabled: boolean;
  checked: boolean;
  ok?: boolean | null;
  reason?: string | null;
  matched_blocked_words: string[];
  review_mode?: string | null;
  risk_tags: string[];
  title?: string | null;
  summary?: string | null;
  subtitle_chars: number;
  checked_at?: string | null;
};

type TaskDetailTab = "overview" | "media" | "subtitle" | "publish" | "logs";
type WorkflowStepState = "done" | "active" | "pending" | "failed";

type WorkflowStep = {
  label: string;
  detail: string;
  state: WorkflowStepState;
};

function normalizeYouTubeSubtitleMode(value: unknown, legacyPrefer?: boolean | null): YouTubeSubtitleMode {
  const mode = String(value ?? "").trim().toLowerCase();
  if (mode === "off" || mode === "target" || mode === "auto_source") return mode;
  if (legacyPrefer === false) return "off";
  return "target";
}

function workflowStepClass(state: WorkflowStepState): string {
  if (state === "done") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (state === "active") return "border-sky-200 bg-sky-50 text-sky-800";
  if (state === "failed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function WorkflowStepper({ steps }: { steps: WorkflowStep[] }) {
  return (
    <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
      {steps.map((step, index) => (
        <div key={step.label} className={`rounded-md border p-3 ${workflowStepClass(step.state)}`}>
          <div className="flex items-center gap-2">
            <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-current text-xs font-semibold">
              {index + 1}
            </span>
            <span className="truncate text-sm font-semibold">{step.label}</span>
          </div>
          <div className="mt-2 min-h-8 text-xs leading-4">{step.detail}</div>
        </div>
      ))}
    </div>
  );
}

export default function TaskDetailPage() {
  const { taskId } = useParams();
  const toast = useToast();
  const confirm = useConfirm();
  const [activeTab, setActiveTab] = useState<TaskDetailTab>("overview");
  const [task, setTask] = useState<Task | null>(null);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [subtitleJobs, setSubtitleJobs] = useState<SubtitleJob[] | null>(null);
  const [publishJobs, setPublishJobs] = useState<PublishJob[] | null>(null);
  const [publishBatches, setPublishBatches] = useState<PublishBatch[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [logSelection, setLogSelection] = useState<string>("combined");
  const [logText, setLogText] = useState<string>("");
  const [logBusy, setLogBusy] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);

  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const [subtitleFormats, setSubtitleFormats] = useState<{ srt: boolean; ass: boolean }>({ srt: true, ass: false });
  const [burnIn, setBurnIn] = useState(false);
  const [softSub, setSoftSub] = useState(false);
  const [videoCodec, setVideoCodec] = useState("av1");
  const [useIntelGpu, setUseIntelGpu] = useState(false);
  const [videoPresetText, setVideoPresetText] = useState<string>("");
  const [videoCrfText, setVideoCrfText] = useState<string>("");
  const [asrEngine, setAsrEngine] = useState("auto");
  const [asrLanguage, setAsrLanguage] = useState("auto");
  const [asrModel, setAsrModel] = useState<string>("");
  const [whisperModels, setWhisperModels] = useState<Array<{ name: string; path: string }> | null>(null);
  const [youtubeSubtitleMode, setYouTubeSubtitleMode] = useState<YouTubeSubtitleMode>("target");
  const [translateEnabled, setTranslateEnabled] = useState(false);
  const [bilingual, setBilingual] = useState(false);
  const [targetLang, setTargetLang] = useState("zh");
  const [translateProvider, setTranslateProvider] = useState("mock");
  const [translateStyle, setTranslateStyle] = useState("口语自然");
  const [translateEnableSummary, setTranslateEnableSummary] = useState(true);
  const [openaiKeySet, setOpenaiKeySet] = useState<boolean | null>(null);

  const [publishMetaText, setPublishMetaText] = useState<string>("{}");
  const [publishPlatform, setPublishPlatform] = useState<PublishPlatform>("bilibili");
  const [publishVideoKey, setPublishVideoKey] = useState<string>("");
  const [publishCoverKey, setPublishCoverKey] = useState<string>("");
  const [publishAccountId, setPublishAccountId] = useState<string>("");
  const [publishSchedule, setPublishSchedule] = useState<string>("");
  const [socialAccounts, setSocialAccounts] = useState<SocialAccount[]>([]);
  const [publishPlatformSettings, setPublishPlatformSettings] = useState<PublishPlatformSettings | null>(null);
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [publishTypeidMode, setPublishTypeidMode] = useState<string>("ai_summary");
  const [publishTypeid, setPublishTypeid] = useState<number | "">("");
  const [publishEnableReprint, setPublishEnableReprint] = useState(true);
  const [biliTypes, setBiliTypes] = useState<BiliTypeNode[] | null>(null);
  const [biliTypesBusy, setBiliTypesBusy] = useState(false);
  const [typeRecommendBusy, setTypeRecommendBusy] = useState(false);
  const [typeRecommend, setTypeRecommend] = useState<BilibiliTypeRecommendResponse | null>(null);
  const [publishReview, setPublishReview] = useState<PublishReview | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [youtubeMeta, setYoutubeMeta] = useState<YouTubeMeta | null>(null);
  const [didAutoPickCover, setDidAutoPickCover] = useState(false);
  const loadedPublishMetaTextRef = useRef<string>("{}");

  const refresh = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!taskId) return;
      if (!opts?.silent) setError(null);
      try {
        const [t, a, sj, pj, pb, pr, accounts, platforms] = await Promise.all([
          fetchJson<Task>(`${ORCHESTRATOR_URL}/tasks/${taskId}`),
          fetchJson<Asset[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/assets`),
          fetchJson<SubtitleJob[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/subtitle_jobs`),
          fetchJson<PublishJob[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_jobs`),
          fetchJson<PublishBatch[]>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_batches`),
          fetchJson<PublishReview>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_review`),
          fetchJson<SocialAccount[]>(`${ORCHESTRATOR_URL}/settings/publish/social/accounts`),
          fetchJson<PublishPlatformSettingsResponse>(`${ORCHESTRATOR_URL}/settings/publish/platforms`),
        ]);
        setTask(t);
        setAssets(a);
        setSubtitleJobs(sj);
        setPublishJobs(pj);
        setPublishBatches(pb);
        setPublishReview(pr);
        setSocialAccounts(accounts);
        setPublishPlatformSettings(platforms.platforms);
      } catch (e: unknown) {
        if (!opts?.silent) setError(e instanceof Error ? e.message : String(e));
      }
    },
    [taskId],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  const applyLoadedPublishMeta = useCallback((meta: any) => {
    const nextText = JSON.stringify(meta ?? {}, null, 2);
    loadedPublishMetaTextRef.current = nextText;
    setPublishMetaText(nextText);
    const copyright = Number((meta as any)?.copyright ?? 1);
    setPublishEnableReprint(copyright === 2);
  }, []);

  const loadPublishDraft = useCallback(async () => {
    if (!taskId) return;
    const resp = await fetchJson<PublishMetaDraftResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_meta/draft`);
    applyLoadedPublishMeta(resp.meta ?? {});
  }, [taskId, applyLoadedPublishMeta]);

  const generatePublishDraft = useCallback(
    async (mode: "default" | "source", meta?: any) => {
      if (!taskId) return;
      const resp = await fetchJson<PublishMetaDraftResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_meta/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, meta: meta ?? null }),
      });
      applyLoadedPublishMeta(resp.meta ?? {});
    },
    [taskId, applyLoadedPublishMeta],
  );

  useEffect(() => {
    if (!taskId) return;
    setPublishVideoKey("");
    setPublishPlatform("bilibili");
    setPublishCoverKey("");
    setPublishAccountId("");
    setPublishSchedule("");
    setCoverFile(null);
    setPublishTypeidMode("ai_summary");
    setPublishTypeid("");
    setBiliTypes(null);
    setTypeRecommend(null);
    setPublishReview(null);
    setYoutubeMeta(null);
    setDidAutoPickCover(false);
    loadedPublishMetaTextRef.current = "{}";
    setPublishMetaText("{}");
    setPublishEnableReprint(true);

    (async () => {
      try {
        await loadPublishDraft();
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
      }
    })();
  }, [taskId, loadPublishDraft]);

  useEffect(() => {
    if (publishPlatform === "bilibili") {
      setPublishAccountId("");
      return;
    }
    const accounts = activeAccountsForPlatform(socialAccounts, publishPlatform).filter((account) => account.check_state === "valid");
    setPublishAccountId((current) => (accounts.some((account) => account.id === current) ? current : accounts[0]?.id ?? ""));
  }, [publishPlatform, socialAccounts]);

  useEffect(() => {
    if (!publishPlatformSettings || publishPlatformSettings[publishPlatform]) return;
    const firstEnabled = (["bilibili", "douyin", "xiaohongshu", "kuaishou"] as PublishPlatform[]).find(
      (platform) => publishPlatformSettings[platform],
    );
    if (firstEnabled) setPublishPlatform(firstEnabled);
  }, [publishPlatform, publishPlatformSettings]);

  const publishPlatformEnabled = Boolean(publishPlatformSettings?.[publishPlatform]);

  useEffect(() => {
    fetchJson<Array<{ name: string; path: string }>>(`${ORCHESTRATOR_URL}/subtitle/models`)
      .then((m) => setWhisperModels(m))
      .catch(() => setWhisperModels(null));
  }, []);

  useEffect(() => {
    if (!taskId) return;
    (async () => {
      try {
        const [profile, translateSettings] = await Promise.all([
          fetchJson<SubtitleAutoProfile>(`${ORCHESTRATOR_URL}/subtitle/auto/profile`),
          fetchJson<{ openai_api_key_set: boolean }>(`${ORCHESTRATOR_URL}/subtitle/translate/settings`),
        ]);

        const formats = Array.isArray(profile.formats) ? profile.formats : [];
        setSubtitleFormats({
          srt: formats.includes("srt"),
          ass: formats.includes("ass"),
        });
        setBurnIn(Boolean(profile.burn_in));
        setSoftSub(Boolean(profile.soft_sub));
        setVideoCodec((profile.video_codec || "av1").toLowerCase());
        setUseIntelGpu(Boolean(profile.use_intel_gpu));
        setVideoPresetText(typeof profile.video_preset === "string" ? profile.video_preset : "");
        setVideoCrfText(typeof profile.video_crf === "number" ? String(profile.video_crf) : "");
        setAsrEngine(profile.asr_engine || "auto");
        setAsrLanguage(profile.asr_language || "auto");
        setAsrModel((profile.asr_model ?? "").trim());
        setYouTubeSubtitleMode(normalizeYouTubeSubtitleMode(profile.youtube_subtitle_mode, profile.prefer_youtube_subtitles));
        setTranslateEnabled(Boolean(profile.translate_enabled));
        setBilingual(Boolean(profile.bilingual));
        setTargetLang(profile.target_lang || "zh");
        setTranslateProvider(profile.translate_provider || "openai");
        setTranslateStyle(profile.translate_style || "口语自然");
        setTranslateEnableSummary(Boolean(profile.translate_enable_summary));
        setPublishTypeidMode((profile.publish_typeid_mode || "ai_summary").toLowerCase());
        setOpenaiKeySet(Boolean(translateSettings.openai_api_key_set));
      } catch {
        // Fallback: only fetch OpenAI key status so UI can show guidance.
        try {
          const s = await fetchJson<{ openai_api_key_set: boolean }>(`${ORCHESTRATOR_URL}/subtitle/translate/settings`);
          setOpenaiKeySet(Boolean(s.openai_api_key_set));
        } catch {}
      }
    })();
  }, [taskId]);

  function clampText(text: string, maxLen: number): string {
    const s = (text ?? "").trim();
    if (s.length <= maxLen) return s;
    if (maxLen <= 1) return s.slice(0, maxLen);
    return s.slice(0, maxLen - 1) + "…";
  }

  function flattenBiliTypeOptions(nodes: BiliTypeNode[] | null): Array<{ id: number; label: string }> {
    const out: Array<{ id: number; label: string }> = [];
    const walk = (node: BiliTypeNode, parents: string[]) => {
      const name = String(node?.name ?? "").trim();
      const next = name ? [...parents, name] : parents;
      const children = Array.isArray(node?.children) ? node.children : [];
      if (children.length) {
        children.forEach((c) => walk(c, next));
        return;
      }
      const id = Number(node?.id ?? 0);
      if (!Number.isFinite(id) || id <= 0) return;
      const label = next.filter(Boolean).join(" / ") || String(id);
      out.push({ id, label });
    };
    (nodes ?? []).forEach((n) => walk(n, []));
    return out;
  }

  function applyPublishTypeid(tid: number) {
    if (!Number.isFinite(tid) || tid <= 0) return;
    try {
      const metaIn = JSON.parse(publishMetaText);
      if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) throw new Error("publish meta must be a JSON object");
      const metaOut = { ...metaIn, typeid: tid };
      setPublishMetaText(JSON.stringify(metaOut, null, 2));
      setPublishTypeid(tid);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  function applyPublishEnableReprint(enabled: boolean) {
    setPublishEnableReprint(enabled);
    try {
      const metaIn = JSON.parse(publishMetaText);
      if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) return;
      const metaOut: any = { ...metaIn, copyright: enabled ? 2 : 1 };
      if (enabled) {
        const src = String(metaIn?.source ?? "").trim() || String(task?.source_url ?? "").trim();
        if (src) metaOut.source = src;
      } else {
        metaOut.source = "";
      }
      setPublishMetaText(JSON.stringify(metaOut, null, 2));
    } catch {}
  }

  function buildCurrentPublishMeta() {
    const metaIn = JSON.parse(publishMetaText);
    if (!metaIn || typeof metaIn !== "object" || Array.isArray(metaIn)) throw new Error("meta must be a JSON object");
    const meta = { ...metaIn } as Record<string, unknown>;
    if (publishTypeidMode === "meta") {
      const tid = typeof publishTypeid === "number" ? publishTypeid : Number(publishTypeid);
      if (Number.isFinite(tid) && tid > 0) meta.typeid = tid;
    }
    return meta;
  }

  const fetchYouTubeMeta = useCallback(async () => {
    if (!taskId) return null;
    const resp = await fetchJson<YouTubeMetaActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_meta`, {
      method: "POST",
    });
    setYoutubeMeta(resp.metadata);
    return resp.metadata;
  }, [taskId]);

  const savePublishMetaFile = useCallback(
    async (meta: any) => {
      if (!taskId) return;
      if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("publish meta must be a JSON object");
      const resp = await fetchJson<PublishMetaStoreResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/publish_meta`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(meta),
      });
      if (resp.meta) applyLoadedPublishMeta(resp.meta);
    },
    [taskId, applyLoadedPublishMeta],
  );

  const isYouTubeTask = task?.source_type === "youtube" && !!(task.source_url ?? "").trim();

  useEffect(() => {
    try {
      const meta = JSON.parse(publishMetaText);
      const tid = Number((meta as any)?.typeid ?? (meta as any)?.tid ?? 0);
      if (Number.isFinite(tid) && tid > 0) setPublishTypeid(tid);
      const copyright = Number((meta as any)?.copyright ?? 1);
      setPublishEnableReprint(copyright === 2);
    } catch {}
  }, [publishMetaText]);

  const rawAsset = useMemo(() => {
    const raws = (assets ?? []).filter((x) => x.kind === "video_raw");
    return raws.length ? raws[raws.length - 1] : null;
  }, [assets]);
  const metadataAsset = useMemo(() => {
    const metas = (assets ?? []).filter((x) => x.kind === "metadata_json");
    return metas.length ? metas[metas.length - 1] : null;
  }, [assets]);
  const finalAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "video_final"), [assets]);
  const subtitleAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "subtitle_srt" || x.kind === "subtitle_ass"), [assets]);
  const coverAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "cover_image"), [assets]);
  const logAssets = useMemo(() => (assets ?? []).filter((x) => x.kind === "log"), [assets]);
  const youtubeDownloadLogAssets = useMemo(() => logAssets.filter((x) => x.storage_key.includes("/youtube_download_")), [logAssets]);
  const subtitleLogAssets = useMemo(() => logAssets.filter((x) => x.storage_key.includes("/subtitle_")), [logAssets]);
  const renderLogAssets = useMemo(() => logAssets.filter((x) => x.storage_key.includes("/render_")), [logAssets]);
  const latestYouTubeDownloadLog = useMemo(
    () => (youtubeDownloadLogAssets.length ? youtubeDownloadLogAssets[youtubeDownloadLogAssets.length - 1] : null),
    [youtubeDownloadLogAssets],
  );
  const latestSubtitleLog = useMemo(
    () => (subtitleLogAssets.length ? subtitleLogAssets[subtitleLogAssets.length - 1] : null),
    [subtitleLogAssets],
  );
  const latestRenderLog = useMemo(() => (renderLogAssets.length ? renderLogAssets[renderLogAssets.length - 1] : null), [renderLogAssets]);
  const selectedLogAsset = useMemo(() => {
    if (logSelection === "combined") return null;
    return logAssets.find((a) => a.id === logSelection) ?? null;
  }, [logAssets, logSelection]);
  const biliTypeOptions = useMemo(() => flattenBiliTypeOptions(biliTypes), [biliTypes]);
  const publishTypeLabel = useMemo(() => {
    const tid = typeof publishTypeid === "number" ? publishTypeid : Number(publishTypeid);
    if (!Number.isFinite(tid) || tid <= 0) return "";
    return biliTypeOptions.find((o) => o.id === tid)?.label ?? String(tid);
  }, [biliTypeOptions, publishTypeid]);
  const failedSubtitleJobs = useMemo(() => (subtitleJobs ?? []).filter((j) => j.status === "failed").length, [subtitleJobs]);
  const runningSubtitleJobs = useMemo(
    () => (subtitleJobs ?? []).filter((j) => j.status === "queued" || j.status === "running").length,
    [subtitleJobs],
  );
  const failedPublishJobs = useMemo(() => (publishJobs ?? []).filter((j) => j.state === "failed" || j.state === "unknown").length, [publishJobs]);
  const runningPublishJobs = useMemo(
    () => (publishJobs ?? []).filter((j) => j.state === "submitting").length,
    [publishJobs],
  );
  const workflowSteps = useMemo<WorkflowStep[]>(() => {
    if (!task) return [];
    const failed = task.status === "FAILED";
    const hasRawVideo = Boolean(rawAsset);
    const hasSubtitle = subtitleAssets.length > 0 || ["SUBTITLE_READY", "RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED"].includes(task.status);
    const hasFinalVideo = finalAssets.length > 0 || ["RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED"].includes(task.status);
    const reviewDone = ["APPROVED", "PUBLISHING", "PUBLISHED"].includes(task.status);
    const published = task.status === "PUBLISHED";
    return [
      {
        label: "入库",
        detail: task.created_at ? new Date(task.created_at).toLocaleString() : "任务已创建",
        state: "done",
      },
      {
        label: "获取视频",
        detail: hasRawVideo ? "已获取原始视频" : task.source_type === "youtube" ? "等待下载或复用源视频" : "等待上传原始视频",
        state: hasRawVideo ? "done" : failed ? "failed" : "active",
      },
      {
        label: "字幕",
        detail: hasSubtitle ? `${subtitleAssets.length || 1} 个字幕产物` : runningSubtitleJobs ? `${runningSubtitleJobs} 个任务运行中` : "等待生成字幕",
        state: hasSubtitle ? "done" : failed && hasRawVideo ? "failed" : hasRawVideo || runningSubtitleJobs ? "active" : "pending",
      },
      {
        label: "渲染",
        detail: hasFinalVideo ? `${finalAssets.length || 1} 个最终视频` : hasSubtitle ? "等待压制最终视频" : "等待字幕阶段完成",
        state: hasFinalVideo ? "done" : failed && hasSubtitle ? "failed" : hasSubtitle ? "active" : "pending",
      },
      {
        label: "审核",
        detail: reviewDone ? "已通过或进入投稿阶段" : hasFinalVideo ? "等待审核或确认投稿信息" : "等待最终视频",
        state: reviewDone ? "done" : failed && hasFinalVideo ? "failed" : hasFinalVideo ? "active" : "pending",
      },
      {
        label: "投稿",
        detail: published ? "已发布" : runningPublishJobs ? `${runningPublishJobs} 个投稿任务运行中` : failedPublishJobs ? `${failedPublishJobs} 个投稿失败` : "等待提交投稿",
        state: published ? "done" : failedPublishJobs ? "failed" : runningPublishJobs ? "active" : reviewDone ? "active" : "pending",
      },
    ];
  }, [failedPublishJobs, finalAssets.length, rawAsset, runningPublishJobs, runningSubtitleJobs, subtitleAssets.length, task]);

  async function loadBilibiliTypes() {
    setBiliTypesBusy(true);
    setError(null);
    try {
      const resp = await fetchJson<BilibiliArchiveTypesResponse>(`${ORCHESTRATOR_URL}/bilibili/archive/types`);
      setBiliTypes(Array.isArray(resp.typelist) ? resp.typelist : []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBiliTypesBusy(false);
    }
  }

  async function recommendBilibiliTypeid() {
    if (!taskId) return;
    setTypeRecommendBusy(true);
    setError(null);
    setTypeRecommend(null);
    try {
      const resp = await fetchJson<BilibiliTypeRecommendResponse>(`${ORCHESTRATOR_URL}/bilibili/archive/type/recommend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId }),
      });
      setTypeRecommend(resp);
      if (resp.ok && resp.typeid && Number(resp.typeid) > 0) {
        applyPublishTypeid(Number(resp.typeid));
        setPublishTypeidMode("meta");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTypeRecommendBusy(false);
    }
  }

  async function runPublishReview() {
    if (!taskId) return;
    setReviewBusy(true);
    setError(null);
    try {
      const meta = buildCurrentPublishMeta();
      await savePublishMetaFile(meta);
      const resp = await fetchJson<PublishReview>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/publish_review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meta }),
      });
      setPublishReview(resp);
      await refresh({ silent: true });
    } catch (e: unknown) {
      try {
        await refresh({ silent: true });
      } catch {}
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReviewBusy(false);
    }
  }

  async function submitPublish(opts?: { skipReview?: boolean; forceRetry?: boolean; platform?: PublishPlatform; accountId?: string }) {
    if (!taskId) return;
    setBusy(true);
    setError(null);
    try {
      const meta = buildCurrentPublishMeta();
      const platform = opts?.platform ?? publishPlatform;
      const accountId = opts?.accountId ?? publishAccountId;
      if (!publishPlatformSettings?.[platform]) {
        throw new Error(`投稿方式 ${platform} 尚未启用，请先到“投稿设置”勾选启用`);
      }
      if (platform === "bilibili") await savePublishMetaFile(meta);
      const payload = buildPublishActionPayload({
        platform,
        accountId,
        videoKey: publishVideoKey,
        coverKey: publishCoverKey,
        meta,
        schedule: publishSchedule.replace("T", " "),
        typeidMode: publishTypeidMode,
        skipReview: Boolean(opts?.skipReview),
        forceRetry: Boolean(opts?.forceRetry),
      });
      const resp = await fetchJson<PublishResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/publish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await refresh();
      toast({ kind: "success", title: "投稿任务已提交", message: `platform=${resp.platform ?? platform} state=${resp.state}` });
    } catch (e: unknown) {
      try {
        await refresh({ silent: true });
      } catch {}
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitSubtitleJob(opts: { resume: boolean }) {
    if (!taskId) return;
    setBusy(true);
    setError(null);
    try {
      if (!rawAsset && isYouTubeTask) {
        const draftWasPristine = publishMetaText === loadedPublishMetaTextRef.current;
        let metaForDraft: any = null;
        if (draftWasPristine) {
          try {
            const parsed = JSON.parse(publishMetaText);
            metaForDraft = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
          } catch {}
        }
        const resp = await fetchJson<YouTubeDownloadActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_download`, {
          method: "POST",
        });
        setYoutubeMeta(resp.metadata);
        if (!publishCoverKey && resp.cover_asset?.storage_key) {
          setPublishCoverKey(resp.cover_asset.storage_key);
          setDidAutoPickCover(true);
        }
        if (draftWasPristine) {
          await generatePublishDraft("source", metaForDraft);
        }
        await refresh();
      }

      let resp: SubtitleActionResponse;
      if (opts.resume) {
        resp = await fetchJson<SubtitleActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/subtitle_resume`, {
          method: "POST",
        });
      } else {
        const formats = [
          subtitleFormats.srt ? "srt" : null,
          subtitleFormats.ass ? "ass" : null,
        ].filter(Boolean);
        const crfRaw = videoCrfText.trim();
        let video_crf: number | null = null;
        if (crfRaw) {
          const n = Number(crfRaw);
          if (!Number.isFinite(n) || !Number.isInteger(n)) throw new Error("video_crf 必须是整数");
          video_crf = n;
        }
        const presetRaw = videoPresetText.trim();

        resp = await fetchJson<SubtitleActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/subtitle`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            formats,
            resume: false,
            burn_in: burnIn,
            soft_sub: softSub,
            ass_style: "clean_white",
            video_codec: videoCodec,
            use_intel_gpu: useIntelGpu,
            video_preset: presetRaw ? presetRaw : null,
            video_crf,
            asr_engine: asrEngine,
            asr_language: asrLanguage,
            asr_model: asrModel.trim() ? asrModel.trim() : null,
            prefer_youtube_subtitles: youtubeSubtitleMode !== "off",
            youtube_subtitle_mode: youtubeSubtitleMode,
            translate_enabled: translateEnabled,
            translate_provider: translateProvider,
            target_lang: targetLang,
            translate_style: translateStyle,
            translate_enable_summary: translateProvider === "openai" ? translateEnableSummary : null,
            bilingual,
          }),
        });
      }
      await refresh();
      toast({ kind: "success", title: opts.resume ? "已继续字幕任务" : "已提交字幕任务", message: resp.job_id });
    } catch (e: unknown) {
      try {
        await refresh({ silent: true });
        await loadLogs({ silent: true });
      } catch {}
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const canResumeSubtitle = useMemo(() => {
    const hasFailedJob = (subtitleJobs ?? []).some((j) => j.status === "failed");
    const isTaskFailed = task?.status === "FAILED";
    return hasFailedJob || isTaskFailed;
  }, [subtitleJobs, task]);

  const shouldPoll = useMemo(() => {
    const hasSubtitleInFlight = (subtitleJobs ?? []).some((j) => j.status === "queued" || j.status === "running");
    const hasPublishInFlight = (publishJobs ?? []).some((j) => j.state === "submitting");
    return hasSubtitleInFlight || hasPublishInFlight;
  }, [subtitleJobs, publishJobs]);

  const nextAction = (() => {
    if (!task) return null;
    if (task.status === "PUBLISHED") {
      return {
        title: "流程已完成",
        description: "任务已经发布。可以回到媒体页下载最终视频，或查看投稿记录。",
        primaryLabel: "查看成品",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("media"),
        secondaryLabel: "投稿记录",
        onSecondary: () => setActiveTab("publish"),
      };
    }
    if (runningSubtitleJobs > 0 || runningPublishJobs > 0) {
      return {
        title: "正在处理",
        description: runningPublishJobs > 0 ? "投稿任务正在提交，日志会自动刷新。" : "字幕或渲染任务正在运行，日志会自动刷新。",
        primaryLabel: "查看日志",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("logs"),
        secondaryLabel: "刷新状态",
        onSecondary: () => refresh({ silent: true }),
      };
    }
    if (task.status === "FAILED" && canResumeSubtitle) {
      return {
        title: "任务失败，可继续",
        description: "检测到失败任务或失败字幕作业。优先从已有产物继续，避免重复下载和重复处理。",
        primaryLabel: "从失败处继续",
        primaryTone: "warning" as const,
        onPrimary: () => submitSubtitleJob({ resume: true }),
        secondaryLabel: "查看日志",
        onSecondary: () => setActiveTab("logs"),
      };
    }
    if (task.status === "FAILED") {
      return {
        title: "任务失败",
        description: "当前没有可自动继续的字幕作业。先查看日志定位失败阶段。",
        primaryLabel: "查看日志",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("logs"),
        secondaryLabel: "媒体资产",
        onSecondary: () => setActiveTab("media"),
      };
    }
    if (!rawAsset) {
      return {
        title: task.source_type === "youtube" ? "获取源视频" : "上传源视频",
        description: task.source_type === "youtube" ? "还没有原始视频资产。先下载 YouTube 视频和元信息。" : "还没有原始视频资产。先上传本地视频文件。",
        primaryLabel: task.source_type === "youtube" ? "去下载视频" : "去上传视频",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("media"),
        secondaryLabel: "查看任务信息",
        onSecondary: () => setActiveTab("overview"),
      };
    }
    if (finalAssets.length === 0) {
      return {
        title: "生成字幕和最终视频",
        description: "源视频已就绪。下一步配置字幕、翻译和渲染参数，然后提交处理。",
        primaryLabel: "去生成字幕",
        primaryTone: "primary" as const,
        onPrimary: () => setActiveTab("subtitle"),
        secondaryLabel: "查看资产",
        onSecondary: () => setActiveTab("media"),
      };
    }
    if (publishReview?.checked && publishReview.ok === false) {
      return {
        title: "审核未通过",
        description: publishReview.reason || "投稿前审核未通过。请调整标题、简介或审核设置后重试。",
        primaryLabel: "处理投稿信息",
        primaryTone: "warning" as const,
        onPrimary: () => setActiveTab("publish"),
        secondaryLabel: "查看日志",
        onSecondary: () => setActiveTab("logs"),
      };
    }
    return {
      title: "配置并提交投稿",
      description: "最终视频已生成。下一步确认封面、标题、分区和审核结果后提交。",
      primaryLabel: "去投稿",
      primaryTone: "primary" as const,
      onPrimary: () => setActiveTab("publish"),
      secondaryLabel: "下载成品",
      onSecondary: () => setActiveTab("media"),
    };
  })();

  const loadLogs = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!taskId) return;
      const silent = Boolean(opts?.silent);
      if (!silent) setLogError(null);
      if (!silent) setLogBusy(true);
      try {
        const fetchTail = async (assetId: string, maxBytes: number) => {
          const resp = await fetch(`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${assetId}/stream`, {
            headers: { Range: `bytes=-${maxBytes}` },
            credentials: "include",
          });
          if (!resp.ok) throw new Error(`日志获取失败：${resp.status} ${resp.statusText}`);
          return await resp.text();
        };

        const maxBytes = 200_000;
        if (logSelection === "combined") {
          const parts: Array<{ title: string; asset: Asset }> = [];
          if (latestYouTubeDownloadLog) {
            parts.push({ title: `YouTube Download · ${latestYouTubeDownloadLog.storage_key}`, asset: latestYouTubeDownloadLog });
          }
          if (latestSubtitleLog) parts.push({ title: `Subtitle · ${latestSubtitleLog.storage_key}`, asset: latestSubtitleLog });
          if (latestRenderLog) parts.push({ title: `Render · ${latestRenderLog.storage_key}`, asset: latestRenderLog });
          if (!parts.length) {
            setLogText("暂无日志（任务开始后会生成 log 资产）");
            return;
          }
          const texts = await Promise.all(parts.map((p) => fetchTail(p.asset.id, maxBytes)));
          const merged = parts
            .map((p, idx) => `===== ${p.title} =====\n${(texts[idx] ?? "").trimEnd()}\n`)
            .join("\n");
          setLogText(merged.trimEnd() + "\n");
          return;
        }

        if (!selectedLogAsset) {
          setLogText("日志资产不存在或已被删除");
          return;
        }

        const text = await fetchTail(selectedLogAsset.id, maxBytes);
        setLogText(text);
      } catch (e: unknown) {
        if (!silent) setLogError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!silent) setLogBusy(false);
      }
    },
    [taskId, logSelection, latestYouTubeDownloadLog, latestSubtitleLog, latestRenderLog, selectedLogAsset],
  );

  useEffect(() => {
    if (logSelection === "combined") return;
    if (selectedLogAsset) return;
    setLogSelection("combined");
  }, [logSelection, selectedLogAsset]);

  const logSnapshotKey = [
    logSelection,
    latestYouTubeDownloadLog?.id ?? "",
    latestSubtitleLog?.id ?? "",
    latestRenderLog?.id ?? "",
    selectedLogAsset?.id ?? "",
  ].join("|");

  useEffect(() => {
    if (!isYouTubeTask) return;
    if (didAutoPickCover) return;
    if (publishCoverKey) return;
    if (!coverAssets.length) return;
    setPublishCoverKey(coverAssets[coverAssets.length - 1].storage_key);
    setDidAutoPickCover(true);
  }, [isYouTubeTask, didAutoPickCover, publishCoverKey, coverAssets]);

  useEffect(() => {
    if (!taskId) return;
    if (shouldPoll) return;
    void loadLogs({ silent: true });
  }, [taskId, shouldPoll, logSnapshotKey, loadLogs]);

  useEffect(() => {
    if (!taskId) return;
    if (!shouldPoll) return;

    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      if (cancelled) return;
      const plan = createTaskDetailPollPlan({
        shouldPoll,
      });
      const jobs: Array<Promise<unknown>> = [];
      if (plan.shouldRefreshTask) jobs.push(refresh({ silent: true }));
      if (plan.shouldLoadLogs) jobs.push(loadLogs({ silent: true }));
      await Promise.allSettled(jobs);
      if (cancelled) return;
      if (plan.nextDelayMs !== null) {
        timer = window.setTimeout(tick, plan.nextDelayMs);
      }
    };

    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [taskId, shouldPoll, refresh, loadLogs]);

  if (!taskId) return null;

  const tabs: Array<{ id: TaskDetailTab; label: string; badge?: string | number | null }> = [
    { id: "overview", label: "概览" },
    { id: "media", label: "媒体与资产", badge: assets?.length ?? null },
    { id: "subtitle", label: "字幕 / 渲染", badge: runningSubtitleJobs || failedSubtitleJobs || null },
    { id: "publish", label: "投稿", badge: publishJobs?.length ?? null },
    { id: "logs", label: "日志", badge: logAssets.length || null },
  ];

  return (
    <div className="space-y-4">
      <div className="vr-section">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-semibold">任务详情</div>
            <div className="mt-1 font-mono text-xs text-slate-600">{taskId}</div>
          </div>
          <Button onClick={() => refresh()}>
            刷新
          </Button>
        </div>

        {error ? <div className="mt-3 whitespace-pre-wrap break-words text-sm text-rose-700">{error}</div> : null}
        {!task ? <div className="mt-3 text-sm text-slate-500">加载中…</div> : null}
        {task ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">状态</div>
              <div className="mt-1">
                <StatusBadge status={task.status} />
              </div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">来源</div>
              <div className="mt-1 text-sm text-slate-800">
                {task.source_type} · {task.source_license}
              </div>
              <div className="mt-1 break-all text-xs text-slate-600">{task.source_url ?? "-"}</div>
            </div>
          </div>
        ) : null}
        {task?.error_message ? (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <div className="text-xs text-amber-700">Task Message</div>
            <div className="mt-1 whitespace-pre-wrap break-words">{task.error_message}</div>
          </div>
        ) : null}

        <div className="mt-4 flex gap-1 overflow-x-auto border-b border-slate-200">
          {tabs.map((tab) => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={[
                  "mb-[-1px] inline-flex shrink-0 items-center gap-2 border-b-2 px-3 py-2 text-sm",
                  active
                    ? "border-slate-900 text-slate-950"
                    : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-950",
                ].join(" ")}
              >
                {tab.label}
                {tab.badge ? <span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">{tab.badge}</span> : null}
              </button>
            );
          })}
        </div>
      </div>

      {activeTab === "overview" ? (
        <div className="space-y-4">
          <div className="vr-section">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">处理流程</div>
                <div className="mt-1 text-xs text-slate-500">按当前任务状态、资产和远程作业推断。</div>
              </div>
              {task ? <StatusBadge status={task.status} /> : null}
            </div>
            <div className="mt-3">{workflowSteps.length ? <WorkflowStepper steps={workflowSteps} /> : <div className="text-sm text-slate-500">等待任务加载。</div>}</div>
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
            <div className="vr-section">
              <div className="text-sm font-semibold">下一步</div>
              {nextAction ? (
                <>
                  <div className="mt-3 text-base font-semibold text-slate-950">{nextAction.title}</div>
                  <div className="mt-1 max-w-3xl text-sm text-slate-600">{nextAction.description}</div>
                  <div className="mt-4 flex flex-wrap items-center gap-2">
                    <Button type="button" tone={nextAction.primaryTone} disabled={busy} onClick={nextAction.onPrimary}>
                      {nextAction.primaryLabel}
                    </Button>
                    {nextAction.secondaryLabel ? (
                      <Button type="button" disabled={busy} onClick={nextAction.onSecondary}>
                        {nextAction.secondaryLabel}
                      </Button>
                    ) : null}
                  </div>
                </>
              ) : (
                <div className="mt-3 text-sm text-slate-500">等待任务加载。</div>
              )}
            </div>

            <div className="vr-section">
              <div className="text-sm font-semibold">当前摘要</div>
              <div className="mt-3 space-y-3 text-sm">
                <button type="button" onClick={() => setActiveTab("media")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">资产</span>
                  <span className="font-medium text-slate-950">{assets?.length ?? "-"}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("subtitle")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">字幕运行 / 失败</span>
                  <span className="font-medium text-slate-950">{runningSubtitleJobs} / {failedSubtitleJobs}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("media")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">最终视频</span>
                  <span className="font-medium text-slate-950">{finalAssets.length}</span>
                </button>
                <button type="button" onClick={() => setActiveTab("publish")} className="flex w-full items-center justify-between gap-3 text-left">
                  <span className="text-slate-500">投稿运行 / 失败</span>
                  <span className={failedPublishJobs ? "font-medium text-rose-700" : "font-medium text-slate-950"}>{runningPublishJobs} / {failedPublishJobs}</span>
                </button>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-slate-500">更新时间</span>
                  <span className="font-medium text-slate-950">{task ? new Date(task.updated_at).toLocaleString() : "-"}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {activeTab === "media" ? (
      <>
      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Upload / Raw Video</div>
        <div className="mt-2 text-xs text-slate-500">已上传：{rawAsset ? rawAsset.storage_key : "无"}</div>
        <div className="mt-1 text-xs text-slate-500">metadata：{metadataAsset ? metadataAsset.storage_key : "无"}</div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input type="file" accept="video/*" onChange={(e) => setVideoFile(e.target.files?.[0] ?? null)} />
          <button
            disabled={!videoFile || busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const fd = new FormData();
                if (!videoFile) throw new Error("no file selected");
                fd.append("file", videoFile, videoFile.name);
                await fetchJson(`${ORCHESTRATOR_URL}/tasks/${taskId}/upload/video`, { method: "POST", body: fd });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            上传
          </button>
          {isYouTubeTask ? (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  const draftWasPristine = publishMetaText === loadedPublishMetaTextRef.current;
                  let metaForDraft: any = null;
                  if (draftWasPristine) {
                    try {
                      const parsed = JSON.parse(publishMetaText);
                      metaForDraft = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
                    } catch {}
                  }
                  const resp = await fetchJson<YouTubeDownloadActionResponse>(`${ORCHESTRATOR_URL}/tasks/${taskId}/actions/youtube_download`, {
                    method: "POST",
                  });
                  setYoutubeMeta(resp.metadata);
                  if (!publishCoverKey && resp.cover_asset?.storage_key) {
                    setPublishCoverKey(resp.cover_asset.storage_key);
                    setDidAutoPickCover(true);
                  }
                  if (draftWasPristine) {
                    await generatePublishDraft("source", metaForDraft);
                  }
                  await refresh();
                } catch (e: unknown) {
                  try {
                    await refresh({ silent: true });
                    await loadLogs({ silent: true });
                  } catch {}
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  setBusy(false);
                }
              }}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
            >
              从 YouTube 下载
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
                {assets.map((a) => (
                  <tr key={a.id} className="border-t">
                    <td className="py-2 pr-3 text-xs">{a.kind}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{a.storage_key}</td>
                    <td className="py-2 pr-3 text-xs text-slate-600">{new Date(a.created_at).toLocaleString()}</td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <a
                          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                          href={`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}/download`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Download
                        </a>
                        {a.kind === "video_final" ? (
                          <button
                            disabled={busy}
                            className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                            onClick={async () => {
                              const ok = await confirm({
                                title: "删除最终视频资产",
                                message: "确定删除该最终视频资产？这会从存储中删除文件。",
                                confirmLabel: "删除",
                                tone: "danger",
                              });
                              if (!ok) return;
                              setBusy(true);
                              setError(null);
                              try {
                                await fetchJson(`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}`, { method: "DELETE" });
                                await refresh();
                              } catch (e: unknown) {
                                setError(e instanceof Error ? e.message : String(e));
                              } finally {
                                setBusy(false);
                              }
                            }}
                          >
                            Delete
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
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
                  href={`${ORCHESTRATOR_URL}/tasks/${taskId}/assets/${a.id}/download`}
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
      ) : null}

      {activeTab === "subtitle" ? (
      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Subtitle</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">输出格式</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.srt}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, srt: e.target.checked }))}
                />
                SRT
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={subtitleFormats.ass}
                  onChange={(e) => setSubtitleFormats((v) => ({ ...v, ass: e.target.checked }))}
                />
                ASS
              </label>
            </div>
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={burnIn} onChange={(e) => setBurnIn(e.target.checked)} />
                硬字幕（burn-in）
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={softSub} onChange={(e) => setSoftSub(e.target.checked)} />
                软字幕（mkv）
              </label>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_codec（硬字幕输出编码）</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={videoCodec} onChange={(e) => setVideoCodec(e.target.value)}>
                  <option value="av1">av1（体积更小，编码更慢）</option>
                  <option value="h264">h264（兼容更好，编码更快）</option>
                </select>
              </label>
              <div className="mt-2 text-xs text-slate-500">提示：只有在启用 “硬字幕（burn-in）” 时才会用到该编码设置。</div>
            </div>
            <div className="mt-3 flex items-center gap-3 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={useIntelGpu}
                  onChange={(e) => setUseIntelGpu(e.target.checked)}
                />
                启用 Intel iGPU 硬件编码
              </label>
            </div>
            <div className="mt-2 text-xs text-slate-500">
              提示：这里只加速硬字幕的最终视频编码。字幕烧录本身仍是 CPU 过滤；软字幕只是封装，不走 GPU。当前 Intel 路径支持 h264/av1。
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_crf（可选：留空=默认）</div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoCrfText}
                  onChange={(e) => setVideoCrfText(e.target.value)}
                  placeholder={videoCodec === "h264" ? "默认 18（h264）" : "默认 24（av1）"}
                />
              </label>
            </div>
            <div className="mt-3">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">video_preset（可选：留空=默认）</div>
                <input
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={videoPresetText}
                  onChange={(e) => setVideoPresetText(e.target.value)}
                  placeholder={
                    useIntelGpu
                      ? videoCodec === "h264"
                        ? "留空=驱动默认（Intel h264）"
                        : "留空=驱动默认（Intel av1）；可填 0..13"
                      : videoCodec === "h264"
                        ? "默认 veryfast（h264）"
                        : "默认 4（av1, 0..13 越小越慢）"
                  }
                />
              </label>
              <div className="mt-2 text-xs text-slate-500">
                提示：CPU h264 可用 ultrafast..veryslow；CPU av1（SVT）为 0..13。Intel GPU 开启时，h264 文本 preset 和 av1 的 0..13 都会映射到 VAAPI quality。
              </div>
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">翻译（可选：mock/noop/openai）</div>
            <div className="mt-2 flex items-center gap-3 text-sm">
              {isYouTubeTask ? (
                <label className="block min-w-64">
                  <div className="mb-1 text-xs text-slate-600">YouTube 字幕复用</div>
                  <select
                    className="w-full rounded border px-3 py-2 text-sm"
                    value={youtubeSubtitleMode}
                    onChange={(e) => setYouTubeSubtitleMode(normalizeYouTubeSubtitleMode(e.target.value))}
                  >
                    <option value="off">关闭，直接走 ASR</option>
                    <option value="target">优先目标语言字幕</option>
                    <option value="auto_source">优先自动生成原语言字幕</option>
                  </select>
                </label>
              ) : null}
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={translateEnabled} onChange={(e) => setTranslateEnabled(e.target.checked)} />
                启用翻译
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={bilingual} onChange={(e) => setBilingual(e.target.checked)} />
                双语
              </label>
            </div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">target_lang</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">provider</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={translateProvider}
                  onChange={(e) => setTranslateProvider(e.target.value)}
                >
                  <option value="mock">mock</option>
                  <option value="noop">noop</option>
                  <option value="openai">openai</option>
                </select>
              </label>

              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">style</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={translateStyle} onChange={(e) => setTranslateStyle(e.target.value)}>
                  <option value="口语自然">口语自然</option>
                  <option value="正式严谨">正式严谨</option>
                  <option value="电商营销">电商营销</option>
                </select>
              </label>

              <label className="flex items-center gap-2 text-sm md:col-span-2">
                <input
                  type="checkbox"
                  checked={translateEnableSummary}
                  onChange={(e) => setTranslateEnableSummary(e.target.checked)}
                  disabled={translateProvider !== "openai"}
                />
                dynamic summary（仅 openai）
              </label>

              {isYouTubeTask ? (
                <div className="md:col-span-2 text-xs text-slate-500">
                  {youtubeSubtitleMode === "off"
                    ? "逻辑：不复用 YouTube 字幕，直接进入 ASR；若启用翻译，则在 ASR 结果上继续翻译。"
                    : youtubeSubtitleMode === "auto_source"
                      ? "逻辑：优先抓取 YouTube 自动生成的原语言字幕；若启用翻译，则直接进入翻译管线；如果没有可用自动字幕，再回退到 ASR。"
                      : "逻辑：优先找 `target_lang` 对应的 YouTube 字幕；命中后直接复用并跳过翻译；如果没有可用目标字幕，再回退到 ASR。"}
                </div>
              ) : null}

              {translateEnabled && translateProvider === "openai" && openaiKeySet === false ? (
                <div className="md:col-span-2 text-xs text-rose-700">
                  OpenAI API Key 未设置，请先到 <Link className="underline" to="/settings/translate">Settings · Translate</Link> 保存配置。
                </div>
              ) : null}
            </div>
          </div>

          <div className="rounded border p-3">
            <div className="text-xs text-slate-500">ASR（语音识别）</div>
            <div className="mt-3 grid gap-2 md:grid-cols-2">
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">engine</div>
                <select className="w-full rounded border px-3 py-2 text-sm" value={asrEngine} onChange={(e) => setAsrEngine(e.target.value)}>
                  <option value="auto">auto（使用后端默认）</option>
                  <option value="mock">mock</option>
                  <option value="faster-whisper">faster-whisper</option>
                  <option value="openvino">openvino（方案2 / Intel Arc）</option>
                </select>
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">language</div>
                <input className="w-full rounded border px-3 py-2 text-sm" value={asrLanguage} onChange={(e) => setAsrLanguage(e.target.value)} placeholder="auto / zh / en ..." />
              </label>
              <label className="block md:col-span-2">
                <div className="mb-1 text-xs text-slate-600">model（可选：本地模型目录路径）</div>
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value={asrModel}
                  onChange={(e) => setAsrModel(e.target.value)}
                >
                  <option value="">(use env default)</option>
                  {(whisperModels ?? []).map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
                <div className="mt-2 text-xs text-slate-500">
                  提示：`faster-whisper` 和 `openvino` 都可以传本地模型目录路径；OpenVINO 需要先准备好已导出的 Whisper 模型目录。
                </div>
              </label>
            </div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            disabled={busy}
            onClick={() => submitSubtitleJob({ resume: false })}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            生成字幕
          </button>
          {canResumeSubtitle ? (
            <button
              disabled={busy}
              onClick={() => submitSubtitleJob({ resume: true })}
              className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
              title="沿用上一次失败任务的完整配置继续执行，保留自动投稿等后续流程。"
            >
              从失败处继续
            </button>
          ) : null}
        </div>

        <div className="mt-4">
          <div className="text-xs font-semibold text-slate-700">Subtitle Jobs</div>
          {!subtitleJobs ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
          {subtitleJobs && subtitleJobs.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
          {subtitleJobs && subtitleJobs.length > 0 ? (
            <div className="mt-2 overflow-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-2 pr-3">ID</th>
                    <th className="py-2 pr-3">Status</th>
                    <th className="py-2 pr-3">Progress</th>
                    <th className="py-2 pr-3">Error</th>
                    <th className="py-2 pr-3">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {subtitleJobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3">{j.status}</td>
                      <td className="py-2 pr-3">{j.progress}%</td>
                      <td
                        className={`py-2 pr-3 text-xs ${j.error_message ? "max-w-[360px] truncate text-rose-700" : "text-slate-400"}`}
                        title={j.error_message ?? ""}
                      >
                        {j.error_message ? clampText(j.error_message, 160) : "—"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>
      ) : null}

      {activeTab === "logs" ? (
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">Logs</div>
          <button
            disabled={logBusy}
            onClick={() => loadLogs()}
            className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
          >
            刷新日志
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select className="rounded border px-3 py-2 text-sm" value={logSelection} onChange={(e) => setLogSelection(e.target.value)}>
            <option value="combined">合并（最新 YouTube 下载 + 字幕 + 压制）</option>
            {[...logAssets].reverse().map((a) => (
              <option key={a.id} value={a.id}>
                {a.storage_key}
              </option>
            ))}
          </select>
          <div className="text-xs text-slate-500">仅显示末尾 200KB；完整内容请在 Assets 里下载。</div>
        </div>
        {logError ? <div className="mt-2 text-xs text-rose-700">{logError}</div> : null}
        <textarea
          readOnly
          value={logText}
          className="mt-2 h-72 w-full rounded border bg-slate-50 p-2 font-mono text-xs text-slate-800"
        />
      </div>
      ) : null}

      {activeTab === "publish" ? (
      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">投稿</div>
        <div className="mt-2 text-xs text-slate-500">
          哔哩哔哩使用现有接口发布；抖音、小红书和快手由独立 SAU 无头浏览器服务发布。
        </div>
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
            <div className="mb-1 text-xs text-slate-600">video_key（可选）</div>
            <select
              className="w-full rounded border px-3 py-2 text-sm"
              value={publishVideoKey}
              onChange={(e) => setPublishVideoKey(e.target.value)}
            >
              <option value="">自动（最新 video_final）</option>
              {[...finalAssets].reverse().map((a) => (
                <option key={a.id} value={a.storage_key}>
                  {a.storage_key}
                </option>
              ))}
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
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const fd = new FormData();
                if (!coverFile) throw new Error("no cover selected");
                fd.append("file", coverFile, coverFile.name);
                const a = await fetchJson<Asset>(`${ORCHESTRATOR_URL}/tasks/${taskId}/upload/cover`, { method: "POST", body: fd });
                setPublishCoverKey(a.storage_key);
                setDidAutoPickCover(true);
                setCoverFile(null);
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
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
                  onClick={() => {
                    const browserUrl = socialPublishBrowserUrl(publishPlatform);
                    if (!browserUrl) return;
                    window.open(
                      new URL(browserUrl, window.location.origin).toString(),
                      "social-publish-douyin",
                      "popup,width=1280,height=860,resizable=yes,scrollbars=yes",
                    );
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
                onClick={() => {
                  try {
                    const meta = JSON.parse(publishMetaText);
                    setPublishMetaText(JSON.stringify(meta, null, 2));
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                }}
              >
                格式化
              </button>
              <button
                disabled={busy || !taskId || publishPlatform !== "bilibili"}
                className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    const meta = JSON.parse(publishMetaText);
                    if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("meta must be a JSON object");
                    await savePublishMetaFile(meta);
                    toast({ kind: "success", title: "已保存", message: "publish_meta.json 已更新。" });
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
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
                  {publishJobs.map((j) => (
                    <tr key={j.id} className="border-t">
                      <td className="py-2 pr-3 font-mono text-xs">{j.id.slice(0, 8)}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.batch_id?.slice(0, 8) ?? "-"}</td>
                      <td className="py-2 pr-3">{j.platform ?? "bilibili"}</td>
                      <td className="py-2 pr-3 font-mono text-xs">{j.account_id?.slice(0, 8) ?? "-"}</td>
                      <td className={`py-2 pr-3 ${j.state === "unknown" || j.state === "submitted" ? "text-amber-700" : ""}`}>{j.state}</td>
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
                            onClick={async () => {
                              const ok = await confirm({
                                title: "确认后重试投稿",
                                message: j.state === "unknown" ? "请确认平台创作者后台没有对应作品。继续可能造成重复投稿。" : "将只重试这个失败渠道。",
                                confirmLabel: "确认重试",
                                tone: "warning",
                              });
                              if (!ok) return;
                              await submitPublish({
                                forceRetry: true,
                                platform: j.platform as PublishPlatform,
                                accountId: j.account_id ?? undefined,
                              });
                            }}
                          >
                            确认后重试
                          </button>
                        ) : "-"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">{new Date(j.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </div>
      ) : null}
    </div>
  );
}
