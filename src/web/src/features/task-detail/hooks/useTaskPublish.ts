import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { useConfirm, useToast } from "../../../components/feedbackContext";
import { desktopApi } from "../../../api/desktop";
import { publishApi } from "../../../api/publish";
import type { PublishJob, Task } from "../../../lib/types";
import { activeAccountsForPlatform, buildPublishActionPayload, type PublishPlatform, type PublishPlatformSettings, type SocialAccount } from "../../../lib/publish";
import type {
  BiliTypeNode,
  BilibiliTypeRecommendResponse,
  PublishReview,
} from "../types";
import { flattenBiliTypeOptions, scopedDesktopUrl } from "../utils";

type UseTaskPublishArgs = {
  taskId: string | undefined;
  task: Task | null;
  publishJobs: PublishJob[] | null;
  setPublishReview: Dispatch<SetStateAction<PublishReview | null>>;
  socialAccounts: SocialAccount[];
  publishPlatformSettings: PublishPlatformSettings | null;
  refresh: (opts?: { silent?: boolean }) => Promise<void>;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusy: Dispatch<SetStateAction<boolean>>;
};

export function useTaskPublish({
  taskId,
  task,
  publishJobs,
  setPublishReview,
  socialAccounts,
  publishPlatformSettings,
  refresh,
  setError,
  setBusy,
}: UseTaskPublishArgs) {
  const toast = useToast();
  const confirm = useConfirm();
  const [publishMetaText, setPublishMetaText] = useState("{}");
  const [publishPlatform, setPublishPlatform] = useState<PublishPlatform>("bilibili");
  const [publishVideoKey, setPublishVideoKey] = useState("");
  const [publishCoverKey, setPublishCoverKey] = useState("");
  const [publishAccountId, setPublishAccountId] = useState("");
  const [publishSchedule, setPublishSchedule] = useState("");
  const [publishTypeidMode, setPublishTypeidMode] = useState("ai_summary");
  const [publishTypeid, setPublishTypeid] = useState<number | "">("");
  const [publishEnableReprint, setPublishEnableReprint] = useState(true);
  const [biliTypes, setBiliTypes] = useState<BiliTypeNode[] | null>(null);
  const [biliTypesBusy, setBiliTypesBusy] = useState(false);
  const [typeRecommendBusy, setTypeRecommendBusy] = useState(false);
  const [typeRecommend, setTypeRecommend] = useState<BilibiliTypeRecommendResponse | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const loadedPublishMetaTextRef = useRef("{}");

  const applyLoadedPublishMeta = useCallback((meta: any) => {
    const nextText = JSON.stringify(meta ?? {}, null, 2);
    loadedPublishMetaTextRef.current = nextText;
    setPublishMetaText(nextText);
    setPublishEnableReprint(Number(meta?.copyright ?? 1) === 2);
  }, []);

  const loadPublishDraft = useCallback(async () => {
    if (!taskId) return;
    const response = await publishApi.getDraft(taskId);
    applyLoadedPublishMeta(response.meta ?? {});
  }, [taskId, applyLoadedPublishMeta]);

  const generatePublishDraft = useCallback(
    async (mode: "default" | "source", meta?: any) => {
      if (!taskId) return;
      const response = await publishApi.generateDraft(taskId, { mode, meta: meta ?? null });
      applyLoadedPublishMeta(response.meta ?? {});
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
    setPublishTypeidMode("ai_summary");
    setPublishTypeid("");
    setBiliTypes(null);
    setTypeRecommend(null);
    setPublishReview(null);
    loadedPublishMetaTextRef.current = "{}";
    setPublishMetaText("{}");
    setPublishEnableReprint(true);
    void loadPublishDraft().catch((error: unknown) => setError(error instanceof Error ? error.message : String(error)));
  }, [taskId, loadPublishDraft, setPublishReview, setError]);

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

  useEffect(() => {
    try {
      const meta = JSON.parse(publishMetaText);
      const typeid = Number(meta?.typeid ?? meta?.tid ?? 0);
      if (Number.isFinite(typeid) && typeid > 0) setPublishTypeid(typeid);
      setPublishEnableReprint(Number(meta?.copyright ?? 1) === 2);
    } catch {}
  }, [publishMetaText]);

  const getPublishDraftInput = useCallback(() => {
    let meta: any = null;
    if (publishMetaText === loadedPublishMetaTextRef.current) {
      try {
        const parsed = JSON.parse(publishMetaText);
        meta = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
      } catch {}
    }
    return { pristine: publishMetaText === loadedPublishMetaTextRef.current, meta };
  }, [publishMetaText]);

  function buildCurrentPublishMeta() {
    const parsed = JSON.parse(publishMetaText);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("meta must be a JSON object");
    const meta = { ...parsed } as Record<string, unknown>;
    if (publishTypeidMode === "meta") {
      const typeid = typeof publishTypeid === "number" ? publishTypeid : Number(publishTypeid);
      if (Number.isFinite(typeid) && typeid > 0) meta.typeid = typeid;
    }
    return meta;
  }

  const savePublishMetaFile = useCallback(
    async (meta: any) => {
      if (!taskId) return;
      if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("publish meta must be a JSON object");
      const response = await publishApi.saveMeta(taskId, meta);
      if (response.meta) applyLoadedPublishMeta(response.meta);
    },
    [taskId, applyLoadedPublishMeta],
  );

  const biliTypeOptions = useMemo(() => flattenBiliTypeOptions(biliTypes), [biliTypes]);
  const publishTypeLabel = useMemo(() => {
    const typeid = typeof publishTypeid === "number" ? publishTypeid : Number(publishTypeid);
    if (!Number.isFinite(typeid) || typeid <= 0) return "";
    return biliTypeOptions.find((option) => option.id === typeid)?.label ?? String(typeid);
  }, [biliTypeOptions, publishTypeid]);
  const publishPlatformEnabled = Boolean(publishPlatformSettings?.[publishPlatform]);
  const failedPublishJobs = useMemo(() => (publishJobs ?? []).filter((job) => job.state === "failed" || job.state === "unknown").length, [publishJobs]);
  const runningPublishJobs = useMemo(() => (publishJobs ?? []).filter((job) => job.state === "submitting").length, [publishJobs]);
  const activeBilibiliUpload = useMemo(
    () => (publishJobs ?? []).find((job) => (job.platform ?? "bilibili") === "bilibili" && Boolean(job.upload_active)) ?? null,
    [publishJobs],
  );

  async function loadBilibiliTypes() {
    setBiliTypesBusy(true);
    setError(null);
    try {
      const response = await publishApi.bilibiliTypes();
      setBiliTypes(Array.isArray(response.typelist) ? response.typelist : []);
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBiliTypesBusy(false);
    }
  }

  function applyPublishTypeid(typeid: number) {
    if (!Number.isFinite(typeid) || typeid <= 0) return;
    try {
      const parsed = JSON.parse(publishMetaText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("publish meta must be a JSON object");
      setPublishMetaText(JSON.stringify({ ...parsed, typeid }, null, 2));
      setPublishTypeid(typeid);
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  function applyPublishEnableReprint(enabled: boolean) {
    setPublishEnableReprint(enabled);
    try {
      const parsed = JSON.parse(publishMetaText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
      const meta = { ...parsed, copyright: enabled ? 2 : 1 } as Record<string, any>;
      if (enabled) {
        const source = String(parsed.source ?? "").trim() || String(task?.source_url ?? "").trim();
        if (source) meta.source = source;
      } else {
        meta.source = "";
      }
      setPublishMetaText(JSON.stringify(meta, null, 2));
    } catch {}
  }

  async function recommendBilibiliTypeid() {
    if (!taskId) return;
    setTypeRecommendBusy(true);
    setError(null);
    setTypeRecommend(null);
    try {
      const response = await publishApi.recommendBilibiliType(taskId);
      setTypeRecommend(response);
      if (response.ok && response.typeid && Number(response.typeid) > 0) {
        applyPublishTypeid(Number(response.typeid));
        setPublishTypeidMode("meta");
      }
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
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
      const response = await publishApi.review(taskId, meta);
      setPublishReview(response);
      await refresh({ silent: true });
    } catch (error: unknown) {
      try {
        await refresh({ silent: true });
      } catch {}
      setError(error instanceof Error ? error.message : String(error));
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
      if (!publishPlatformSettings?.[platform]) throw new Error(`投稿方式 ${platform} 尚未启用，请先到“投稿设置”勾选启用`);
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
      const response = await publishApi.submit(taskId, payload);
      await refresh();
      toast({ kind: "success", title: "投稿任务已提交", message: `platform=${response.platform ?? platform} state=${response.state}` });
    } catch (error: unknown) {
      try {
        await refresh({ silent: true });
      } catch {}
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function openPublishDesktop(browserUrl: string, resourceId: string) {
    const popup = window.open("about:blank", "social-publish-douyin", "popup,width=1280,height=860,resizable=yes,scrollbars=yes");
    if (!popup) throw new Error("浏览器阻止了自动化窗口，请允许弹出窗口后重试");
    try {
      const grant = await desktopApi.createGrant({ desktop_type: "publish", resource_id: resourceId });
      popup.location.replace(scopedDesktopUrl(browserUrl, grant));
    } catch (error) {
      popup.close();
      throw error;
    }
  }

  async function savePublishMetaFromText() {
    setBusy(true);
    setError(null);
    try {
      const meta = JSON.parse(publishMetaText);
      if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("meta must be a JSON object");
      await savePublishMetaFile(meta);
      toast({ kind: "success", title: "已保存", message: "publish_meta.json 已更新。" });
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  function formatPublishMeta() {
    try {
      setPublishMetaText(JSON.stringify(JSON.parse(publishMetaText), null, 2));
    } catch (error: unknown) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  async function retryPublishJob(job: PublishJob) {
    const ok = await confirm({
      title: "确认后重试投稿",
      message: job.state === "unknown" ? "请确认平台创作者后台没有对应作品。继续可能造成重复投稿。" : "将只重试这个失败渠道。",
      confirmLabel: "确认重试",
      tone: "warning",
    });
    if (!ok) return;
    await submitPublish({ forceRetry: true, platform: job.platform as PublishPlatform, accountId: job.account_id ?? undefined });
  }

  return {
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
    publishTypeidMode,
    setPublishTypeidMode,
    publishTypeid,
    publishEnableReprint,
    setPublishEnableReprint,
    biliTypesBusy,
    typeRecommendBusy,
    typeRecommend,
    reviewBusy,
    generatePublishDraft,
    savePublishMetaFile,
    getPublishDraftInput,
    biliTypeOptions,
    publishTypeLabel,
    publishPlatformEnabled,
    failedPublishJobs,
    runningPublishJobs,
    activeBilibiliUpload,
    loadBilibiliTypes,
    recommendBilibiliTypeid,
    applyPublishTypeid,
    applyPublishEnableReprint,
    runPublishReview,
    submitPublish,
    openPublishDesktop,
    savePublishMetaFromText,
    formatPublishMeta,
    retryPublishJob,
  };
}
