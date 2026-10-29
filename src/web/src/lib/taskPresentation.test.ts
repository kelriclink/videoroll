import { describe, expect, it } from "vitest";
import { presentTaskError, taskPresentation } from "./taskPresentation";
import type { TaskStatus } from "./types";

const allStatuses: TaskStatus[] = [
  "CREATED", "INGESTED", "DOWNLOADED", "AUDIO_EXTRACTED", "ASR_DONE", "TRANSLATED",
  "SUBTITLE_READY", "RENDERED", "READY_FOR_REVIEW", "APPROVED", "PUBLISHING", "PUBLISHED",
  "FAILED", "CANCELED",
];

describe("taskPresentation", () => {
  it("maps every backend status to a user-facing label", () => {
    for (const status of allStatuses) {
      const value = taskPresentation(status);
      expect(value.label).toBeTruthy();
      expect(value.shortLabel).toBeTruthy();
      expect(value.label).not.toBe(status);
      expect(value.shortLabel).not.toBe(status);
    }
  });

  it("marks attention and terminal states explicitly", () => {
    expect(taskPresentation("FAILED").needsAttention).toBe(true);
    expect(taskPresentation("PUBLISHED").terminal).toBe(true);
    expect(taskPresentation("ASR_DONE").inProgress).toBe(true);
  });
});

describe("presentTaskError", () => {
  it("summarizes model availability failures", () => {
    expect(presentTaskError("503 model_not_found: No available channel for model gpt-6-luna")?.title)
      .toBe("翻译模型当前不可用");
  });

  it("summarizes numeric validation failures", () => {
    expect(presentTaskError("translation validation failed: idx=42:number_mismatch")?.title)
      .toBe("翻译数字校验未通过");
  });

  it("summarizes timeouts", () => {
    expect(presentTaskError("The read operation timed out")?.title).toBe("上游服务响应超时");
  });

  it("summarizes missing resume inputs", () => {
    expect(presentTaskError("SUBTITLE_INPUT_MISSING: source media missing")?.title)
      .toBe("继续处理所需的源文件已缺失");
  });

  it("falls back without exposing the raw error as the title", () => {
    const raw = "some low level stack trace";
    const value = presentTaskError(raw);
    expect(value?.title).toBe("任务执行失败");
    expect(value?.title).not.toContain(raw);
  });
});
