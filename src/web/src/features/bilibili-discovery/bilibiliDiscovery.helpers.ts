export function formatBilibiliCount(value: number): string {
  const count = Number.isFinite(value) ? Math.max(0, value) : 0;
  if (count >= 100_000_000) return `${(count / 100_000_000).toFixed(1)}亿`;
  if (count >= 10_000) return `${(count / 10_000).toFixed(1)}万`;
  return Math.trunc(count).toLocaleString("zh-CN");
}

export function formatBilibiliTime(iso: string | null): string {
  if (!iso) return "未更新";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN");
}
