import { describe, expect, it } from "vitest";

import { knowledgeItemHref } from "./DashboardPage";

describe("knowledgeItemHref", () => {
  it("creates an item query deep link for the knowledge-base page", () => {
    expect(knowledgeItemHref("knowledge item/1")).toBe("/knowledge?item=knowledge+item%2F1");
  });
});
