export type YouTubeBatchFailure = {
  url: string;
  message: string;
};

export function parseYouTubeUrlLines(value: string): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];

  for (const line of value.split(/\r?\n/)) {
    const url = line.trim();
    if (!url || seen.has(url)) continue;
    seen.add(url);
    urls.push(url);
  }

  return urls;
}

export function formatYouTubeBatchFailure(
  total: number,
  successCount: number,
  failures: YouTubeBatchFailure[],
  maxDetails = 3,
): string {
  const details = failures
    .slice(0, maxDetails)
    .map((failure) => `${failure.url}: ${failure.message}`)
    .join("；");
  const omitted = failures.length - maxDetails;
  const suffix = omitted > 0 ? `；另有 ${omitted} 个失败链接` : "";
  return `批量创建完成：共 ${total} 个，成功 ${successCount} 个，失败 ${failures.length} 个。${details}${suffix}`;
}
