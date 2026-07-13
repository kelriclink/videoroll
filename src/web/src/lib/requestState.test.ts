import { describe, expect, it } from "vitest";

import {
  applyServerValue,
  createDirtyFieldState,
  editDirtyField,
  loadSlices,
  markDirtyFieldConflict,
  reloadDirtyField,
} from "./requestState";

describe("loadSlices", () => {
  it("keeps the core usable when an optional publisher slice fails", async () => {
    const result = await loadSlices(
      async () => ({ task: "task-1" }),
      {
        publishJobs: async () => {
          throw new Error("publisher unavailable");
        },
        review: async () => ({ ok: true }),
      },
    );
    expect(result.core).toEqual({ ok: true, value: { task: "task-1" } });
    expect(result.optional.publishJobs.ok).toBe(false);
    expect(result.optional.review).toEqual({ ok: true, value: { ok: true } });
    expect(result.errors.optional.publishJobs?.message).toContain("publisher unavailable");
  });
});

describe("DirtyFieldState", () => {
  it("does not replace dirty metadata from a background refresh", () => {
    const state = editDirtyField(createDirtyFieldState("server", 1), "local");
    expect(applyServerValue(state, "new server", 2)).toMatchObject({
      value: "local",
      dirty: true,
      conflict: { serverValue: "new server", serverVersion: 2 },
    });
  });

  it("can reload a server version after a save conflict", () => {
    const conflict = markDirtyFieldConflict(editDirtyField(createDirtyFieldState("old", 1), "local"), "server", 2);
    expect(reloadDirtyField(conflict)).toEqual(createDirtyFieldState("server", 2));
  });
});
