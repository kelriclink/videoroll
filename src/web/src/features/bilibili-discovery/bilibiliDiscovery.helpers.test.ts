import { describe, expect, it } from "vitest";
import { formatBilibiliCount, formatBilibiliTime } from "./bilibiliDiscovery.helpers";

describe("Bilibili discovery formatting", () => {
  it("formats play and like numbers using Chinese units", () => {
    expect(formatBilibiliCount(999)).toBe("999");
    expect(formatBilibiliCount(25000)).toBe("2.5万");
    expect(formatBilibiliCount(123000000)).toBe("1.2亿");
    expect(formatBilibiliCount(-1)).toBe("0");
  });

  it("handles unavailable update times", () => {
    expect(formatBilibiliTime(null)).toBe("未更新");
    expect(formatBilibiliTime("invalid")).toBe("时间未知");
    expect(formatBilibiliTime("2026-11-13T00:00:00Z")).toMatch(/2026/);
  });
});
