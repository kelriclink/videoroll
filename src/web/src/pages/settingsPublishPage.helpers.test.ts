import { describe, expect, it } from "vitest";

import { activeAccountsForPlatform, storageStateGuidance } from "./settingsPublishPage.helpers";
import * as helpers from "./settingsPublishPage.helpers";

describe("storageStateGuidance", () => {
  it("shows the exact local SAU command and file path", () => {
    expect(storageStateGuidance("douyin", "creator")).toEqual({
      command: "sau douyin login --account creator",
      path: "cookies/douyin_creator.json",
    });
  });
});

describe("activeAccountsForPlatform", () => {
  it("keeps only active accounts for the selected platform", () => {
    expect(
      activeAccountsForPlatform(
        [
          { id: "1", platform: "douyin", name: "a", is_active: true, check_state: "valid" },
          { id: "2", platform: "douyin", name: "b", is_active: false, check_state: "valid" },
          { id: "3", platform: "kuaishou", name: "c", is_active: true, check_state: "valid" },
        ],
        "douyin",
      ),
    ).toHaveLength(1);
  });
});

describe("loginSessionLabel", () => {
  it("explains when interactive verification is required", () => {
    const label = (helpers as any).loginSessionLabel({ state: "running", message: "browser opened" });
    expect(label).toContain("登录窗口");
    expect(label).toContain("browser opened");
  });
});

describe("enabledPublishPlatforms", () => {
  it("returns only platforms explicitly checked in settings", () => {
    const enabledPublishPlatforms = (helpers as any).enabledPublishPlatforms;
    expect(typeof enabledPublishPlatforms).toBe("function");
    expect(
      enabledPublishPlatforms({
        bilibili: false,
        douyin: true,
        xiaohongshu: false,
        kuaishou: true,
      }),
    ).toEqual(["douyin", "kuaishou"]);
  });
});
