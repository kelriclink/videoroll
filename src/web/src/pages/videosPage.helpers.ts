export type BulkDeleteTarget = {
  assetId: string;
  label: string;
};

export type BulkDeleteFailure = BulkDeleteTarget & {
  message: string;
};

export type BulkDeleteSummary = {
  total: number;
  successCount: number;
  failureCount: number;
  failures: BulkDeleteFailure[];
};

function errorMessage(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  return String(reason);
}

export function summarizeBulkDeleteResults(
  targets: BulkDeleteTarget[],
  results: PromiseSettledResult<unknown>[],
): BulkDeleteSummary {
  const failures: BulkDeleteFailure[] = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") return;
    const target = targets[index];
    failures.push({
      assetId: target.assetId,
      label: target.label,
      message: errorMessage(result.reason),
    });
  });

  return {
    total: targets.length,
    successCount: targets.length - failures.length,
    failureCount: failures.length,
    failures,
  };
}

export function formatBulkDeleteSummary(summary: BulkDeleteSummary, maxFailures = 3): string {
  if (summary.failureCount === 0) {
    return `批量删除完成：成功 ${summary.successCount} 个。`;
  }
  const detail = summary.failures
    .slice(0, maxFailures)
    .map((failure) => `${failure.label}: ${failure.message}`)
    .join("；");
  const suffix = summary.failureCount > maxFailures ? `；其余 ${summary.failureCount - maxFailures} 个失败项请重试。` : "";
  return `批量删除完成：成功 ${summary.successCount} 个，失败 ${summary.failureCount} 个。${detail}${suffix}`;
}
