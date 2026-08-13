import { describe, expect, it } from "vitest";

import { formatYouTubeBatchFailure, parseYouTubeUrlLines } from "./taskNewPage.helpers";

describe("parseYouTubeUrlLines", () => {
  it("parses one URL per line, trims blanks, and removes duplicates", () => {
    expect(
      parseYouTubeUrlLines(
        " https://www.youtube.com/watch?v=first \r\n\nhttps://youtu.be/second\nhttps://www.youtube.com/watch?v=first\n",
      ),
    ).toEqual(["https://www.youtube.com/watch?v=first", "https://youtu.be/second"]);
  });

  it("returns an empty list for blank input", () => {
    expect(parseYouTubeUrlLines(" \n\r\n ")).toEqual([]);
  });
});

describe("formatYouTubeBatchFailure", () => {
  it("summarizes partial success and limits failure details", () => {
    expect(
      formatYouTubeBatchFailure(
        5,
        1,
        [
          { url: "url-1", message: "invalid" },
          { url: "url-2", message: "timeout" },
          { url: "url-3", message: "failed" },
          { url: "url-4", message: "unavailable" },
        ],
      ),
    ).toBe(
      "批量创建完成：共 5 个，成功 1 个，失败 4 个。url-1: invalid；url-2: timeout；url-3: failed；另有 1 个失败链接",
    );
  });
});
