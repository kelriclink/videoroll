import { describe, expect, it } from "vitest";
import { buildBilibiliMultipartParts } from "./publish";
import type { Asset, DubbingJob } from "./types";

const assets = [
  { id: "p1", kind: "video_final", task_id: "task1", storage_key: "task/p1.mp4" },
  { id: "raw", kind: "video_raw", task_id: "task1", storage_key: "task/raw.webm" },
  { id: "p2", kind: "video_dubbed", task_id: "task1", storage_key: "task/p2.mp4" },
] as Asset[];

const job = {
  id: "job1",
  task_id: "task1",
  status: "succeeded",
  output: { subtitle_video_key: "task/p1.mp4", video_key: "task/p2.mp4" },
} as DubbingJob;

describe("Bilibili multipart selection", () => {
  it("submits one ordered P1/P2 pair", () => {
    const result = buildBilibiliMultipartParts([job], assets);
    expect(result.video_key).toBe("task/p1.mp4");
    expect(result.video_parts?.map((part) => part.video.key)).toEqual(["task/p1.mp4", "task/p2.mp4"]);
  });

  it("accepts native YouDub whose subtitle_video_key is the RAW input rather than P1", () => {
    const native = { ...job, output: { subtitle_video_key: "task/raw.webm", video_key: "task/p2.mp4" } } as DubbingJob;
    expect(buildBilibiliMultipartParts([native], assets).video_parts?.[1].video.key).toBe("task/p2.mp4");
  });

  it("rejects an input key that is not registered to this task", () => {
    const foreign = { ...job, output: { subtitle_video_key: "other/raw.webm", video_key: "task/p2.mp4" } } as DubbingJob;
    expect(() => buildBilibiliMultipartParts([foreign], assets)).toThrow();
  });

  it("supports current unified audio-only P2 without legacy subtitle_video_key", () => {
    const currentJob = { ...job, output: { video_key: "task/p2.mp4", source_video_key: "task/raw.webm" } } as DubbingJob;
    const result = buildBilibiliMultipartParts([currentJob], assets);
    expect(result.video_key).toBe("task/p1.mp4");
    expect(result.video_parts?.[1].video.key).toBe("task/p2.mp4");
  });

  it("refuses P2 from another task even when storage keys overlap", () => {
    const wrong = { ...job, task_id: "other", output: { video_key: "task/p2.mp4" } } as DubbingJob;
    expect(() => buildBilibiliMultipartParts([wrong], assets)).toThrow();
  });

  it("refuses P2 without matching P1", () => {
    expect(() => buildBilibiliMultipartParts([job], assets, "other/p1.mp4")).toThrow();
  });

  it("refuses a failed or assetless dubbing job", () => {
    expect(() => buildBilibiliMultipartParts([{ ...job, status: "failed" }], assets)).toThrow();
    expect(() => buildBilibiliMultipartParts([job], assets.slice(0, 1))).toThrow();
  });

  it("does not silently mix P1/P2 from different dubbing jobs", () => {
    const other = {
      ...job,
      id: "job2",
      output: { subtitle_video_key: "task/p1-old.mp4", video_key: "task/p2.mp4" },
    } as DubbingJob;
    expect(() => buildBilibiliMultipartParts([other], assets)).toThrow();
  });
});
