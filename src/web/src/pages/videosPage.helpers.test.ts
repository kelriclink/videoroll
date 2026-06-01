import { describe, expect, it } from "vitest";

import { formatBulkDeleteSummary, summarizeBulkDeleteResults } from "./videosPage.helpers";

describe("summarizeBulkDeleteResults", () => {
  it("counts successful deletions", () => {
    const summary = summarizeBulkDeleteResults(
      [
        { assetId: "a1", label: "video-1" },
        { assetId: "a2", label: "video-2" },
      ],
      [{ status: "fulfilled", value: null }, { status: "fulfilled", value: null }],
    );

    expect(summary).toEqual({
      total: 2,
      successCount: 2,
      failureCount: 0,
      failures: [],
    });
    expect(formatBulkDeleteSummary(summary)).toBe("批量删除完成：成功 2 个。");
  });

  it("keeps failure details and formats a retry message", () => {
    const summary = summarizeBulkDeleteResults(
      [
        { assetId: "a1", label: "video-1" },
        { assetId: "a2", label: "video-2" },
        { assetId: "a3", label: "video-3" },
      ],
      [
        { status: "fulfilled", value: null },
        { status: "rejected", reason: new Error("500 Internal Server Error") },
        { status: "rejected", reason: "timeout" },
      ],
    );

    expect(summary.successCount).toBe(1);
    expect(summary.failureCount).toBe(2);
    expect(summary.failures).toEqual([
      { assetId: "a2", label: "video-2", message: "500 Internal Server Error" },
      { assetId: "a3", label: "video-3", message: "timeout" },
    ]);
    expect(formatBulkDeleteSummary(summary)).toBe(
      "批量删除完成：成功 1 个，失败 2 个。video-2: 500 Internal Server Error；video-3: timeout",
    );
  });
});
