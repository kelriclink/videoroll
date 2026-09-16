import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";
import type { PublishPlatformSettings, SocialAccount } from "../lib/publish";
import type { PublishActionPayload } from "../lib/types";

export type PublishMetaDraftResponse = { meta: any };
export type PublishMetaStoreResponse = { stored: boolean; key: string; meta?: any };
export type PublishResponse = {
  job_id?: string | null;
  state: string;
  platform?: string | null;
  aid?: string | null;
  bvid?: string | null;
  external_id?: string | null;
  external_url?: string | null;
  response?: any;
};
export type BiliTypeNode = { id: number; name: string; children?: BiliTypeNode[] };
export type BilibiliArchiveTypesResponse = { typelist: BiliTypeNode[] };
export type BilibiliTypeRecommendResponse = {
  ok: boolean;
  typeid?: number | null;
  path?: string | null;
  reason?: string;
  used_text?: string;
};
export type PublishReview = {
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

export const publishApi = {
  getDraft(taskId: string) {
    return fetchJson<PublishMetaDraftResponse>(orchestratorUrl(`/tasks/${taskId}/publish_meta/draft`));
  },

  generateDraft(taskId: string, payload: { mode: "default" | "source"; meta: any }) {
    return fetchJson<PublishMetaDraftResponse>(orchestratorUrl(`/tasks/${taskId}/publish_meta/draft`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  saveMeta(taskId: string, meta: Record<string, unknown>) {
    return fetchJson<PublishMetaStoreResponse>(orchestratorUrl(`/tasks/${taskId}/publish_meta`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meta),
    });
  },

  bilibiliTypes() {
    return fetchJson<BilibiliArchiveTypesResponse>(orchestratorUrl("/bilibili/archive/types"));
  },

  recommendBilibiliType(taskId: string) {
    return fetchJson<BilibiliTypeRecommendResponse>(orchestratorUrl("/bilibili/archive/type/recommend"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    });
  },

  review(taskId: string, meta: Record<string, unknown>) {
    return fetchJson<PublishReview>(orchestratorUrl(`/tasks/${taskId}/actions/publish_review`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meta }),
    });
  },

  submit(taskId: string, payload: PublishActionPayload) {
    return fetchJson<PublishResponse>(orchestratorUrl(`/tasks/${taskId}/actions/publish`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  accounts() {
    return fetchJson<SocialAccount[]>(orchestratorUrl("/settings/publish/social/accounts"));
  },

  platformSettings() {
    return fetchJson<{ platforms: PublishPlatformSettings }>(orchestratorUrl("/settings/publish/platforms"));
  },
};
