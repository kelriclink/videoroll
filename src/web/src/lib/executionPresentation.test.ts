import { describe, expect, it } from "vitest";
import { jobStatusLabel, publishPlatformLabel, queueStageLabel, queueStateLabel } from "./executionPresentation";

describe("execution presentation", () => {
  it("uses product language for queue state and stages", () => {
    expect(queueStateLabel("running")).toBe("处理中");
    expect(queueStateLabel("queued")).toBe("等待处理");
    expect(queueStageLabel("subtitle")).toBe("制作字幕");
    expect(queueStageLabel("render")).toBe("生成视频");
  });

  it("hides unknown internal job state behind a stable label", () => {
    expect(jobStatusLabel("succeeded")).toBe("已完成");
    expect(jobStatusLabel("brand_new_internal_state")).toBe("状态未知");
  });

  it("renders publish platform names", () => {
    expect(publishPlatformLabel("bilibili")).toBe("哔哩哔哩");
  });
});
