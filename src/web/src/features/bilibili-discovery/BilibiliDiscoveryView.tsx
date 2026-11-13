import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button, InlineNotice, Section } from "../../components/ui";
import { fetchJson } from "../../lib/http";
import { ORCHESTRATOR_URL } from "../../lib/urls";
import { formatBilibiliCount, formatBilibiliTime } from "./bilibiliDiscovery.helpers";

type RankingVideo = {
  rank: number;
  bvid: string;
  title: string;
  uploader: string;
  category: string;
  cover: string | null;
  views: number;
  likes: number;
  danmaku: number;
  url: string;
};

type PublicActivity = {
  id: number;
  title: string;
  description: string;
  start_at: string;
  end_at: string;
  cover: string | null;
  url: string;
  eligibility: "unknown";
};

type Feed<T> = {
  items: T[];
  fetched_at: string | null;
  source: string | null;
  stale: boolean;
  error: string | null;
};

type Discovery = {
  ranking: Feed<RankingVideo>;
  activities: Feed<PublicActivity>;
  note: string;
};

function DataState({ data, label }: { data: Feed<unknown> | null; label: string }) {
  if (!data) return <p className="py-6 text-sm text-slate-500">正在加载{label}…</p>;
  if (data.error) {
    return <InlineNotice tone="warning">{data.error}{data.stale ? "（以下为历史快照）" : ""}</InlineNotice>;
  }
  if (data.items.length === 0) return <p className="py-6 text-sm text-slate-500">当前暂无可显示的{label}。</p>;
  return null;
}

function RankingList({ data, limit }: { data: Feed<RankingVideo> | null; limit: number }) {
  return (
    <div className="space-y-2">
      <DataState data={data} label="B站排行榜" />
      {data?.items.slice(0, limit).map((item) => (
        <a
          key={item.bvid}
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex min-w-0 gap-3 rounded-lg border border-slate-100 p-2.5 transition hover:border-sky-200 hover:bg-sky-50/60 dark:border-slate-800 dark:hover:border-sky-800 dark:hover:bg-slate-800/40"
        >
          <div className="flex w-5 shrink-0 items-start justify-center pt-1 text-sm font-semibold text-slate-500">{item.rank}</div>
          {item.cover ? (
            <img
              src={item.cover}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              className="h-16 w-24 shrink-0 rounded-md bg-slate-100 object-cover dark:bg-slate-800 sm:h-[72px] sm:w-28"
            />
          ) : null}
          <div className="min-w-0 flex-1">
            <div className="line-clamp-2 text-sm font-medium text-slate-900 group-hover:text-sky-700 dark:text-white dark:group-hover:text-sky-300">{item.title}</div>
            <div className="mt-1 truncate text-xs text-slate-500 dark:text-slate-400">{item.uploader || "UP主未知"}{item.category ? ` · ${item.category}` : ""}</div>
            <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500 dark:text-slate-400">
              <span>{formatBilibiliCount(item.views)} 播放</span>
              <span>{formatBilibiliCount(item.likes)} 点赞</span>
            </div>
          </div>
          <span aria-hidden="true" className="shrink-0 text-slate-400">↗</span>
        </a>
      ))}
    </div>
  );
}

function ActivityList({ data, limit }: { data: Feed<PublicActivity> | null; limit: number }) {
  return (
    <div className="space-y-2">
      <DataState data={data} label="公开活动" />
      {data?.items.slice(0, limit).map((item) => (
        <a
          key={item.id}
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          className="group flex min-w-0 gap-3 rounded-lg border border-slate-100 p-3 transition hover:border-sky-200 hover:bg-sky-50/60 dark:border-slate-800 dark:hover:border-sky-800 dark:hover:bg-slate-800/40"
        >
          {item.cover ? (
            <img src={item.cover} alt="" loading="lazy" referrerPolicy="no-referrer" className="h-16 w-20 shrink-0 rounded-md bg-slate-100 object-cover dark:bg-slate-800 sm:w-24" />
          ) : null}
          <div className="min-w-0 flex-1">
            <div className="line-clamp-2 text-sm font-medium text-slate-900 group-hover:text-sky-700 dark:text-white dark:group-hover:text-sky-300">{item.title}</div>
            {item.description ? <p className="mt-1 line-clamp-1 text-xs text-slate-500 dark:text-slate-400">{item.description}</p> : null}
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
              <span>截止 {formatBilibiliTime(item.end_at)}</span>
              <span>报名资格未核实</span>
            </div>
          </div>
          <span aria-hidden="true" className="shrink-0 text-slate-400">↗</span>
        </a>
      ))}
    </div>
  );
}

export default function BilibiliDiscoveryView({ compact = false }: { compact?: boolean }) {
  const [data, setData] = useState<Discovery | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (force: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchJson<Discovery>(`${ORCHESTRATOR_URL}/bilibili/discovery${force ? "?refresh=true" : ""}`);
      setData(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "获取 B站动态失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(false); }, [load]);

  return (
    <Section>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-950 dark:text-white">B站热点与活动</h2>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            B站官方排行榜（失败时使用热门综合）与公开活动目录 · 缓存约 5 分钟
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {compact ? <Link to="/bilibili/discovery" className="text-xs text-sky-700 hover:underline dark:text-sky-300">查看全部</Link> : null}
          <Button size="xs" type="button" disabled={loading} onClick={() => void load(true)}>
            {loading ? "获取中…" : "刷新"}
          </Button>
        </div>
      </div>
      {error ? <div className="mt-3"><InlineNotice tone="warning">数据请求失败，已保留之前加载的内容。{error}</InlineNotice></div> : null}
      <div className="mt-4 grid grid-cols-1 gap-5 xl:grid-cols-2">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium text-slate-800 dark:text-slate-200">{data?.ranking.source === "热门综合" ? "热门视频 · 热门综合" : "热门视频 · 排行榜"}</h3>
            <span className="text-xs text-slate-400">更新 {formatBilibiliTime(data?.ranking.fetched_at ?? null)}</span>
          </div>
          <RankingList data={data?.ranking ?? null} limit={compact ? 5 : 20} />
        </div>
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium text-slate-800 dark:text-slate-200">当前公开活动</h3>
            <span className="text-xs text-slate-400">更新 {formatBilibiliTime(data?.activities.fetched_at ?? null)}</span>
          </div>
          <ActivityList data={data?.activities ?? null} limit={compact ? 5 : 30} />
        </div>
      </div>
      <p className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
        {data?.note ?? "公开活动仅供浏览，并不代表已报名、符合活动规则或可在投稿时使用 mission_id。"}
        {data?.ranking.stale || data?.activities.stale ? " 部分信息来自旧缓存。" : ""}
      </p>
    </Section>
  );
}
