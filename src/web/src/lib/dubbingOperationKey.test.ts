import { afterEach, describe, expect, it, vi } from "vitest";
import { createYouDubOperationKey } from "./dubbingOperationKey";

afterEach(() => vi.unstubAllGlobals());

describe("YouDub task idempotency key", () => {
  it("works on LAN HTTP where crypto.randomUUID is unavailable", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        for (let index = 0; index < bytes.length; index += 1) bytes[index] = index;
        return bytes;
      },
    });
    expect(createYouDubOperationKey()).toMatch(/^youdub-native:[a-z0-9]+:000102030405060708090a0b0c0d0e0f$/);
  });

  it("still works in older browsers without crypto", () => {
    vi.stubGlobal("crypto", undefined);
    expect(createYouDubOperationKey()).toMatch(/^youdub-native:[a-z0-9]+:[a-z0-9]+$/);
  });

  it("uses a fresh random suffix per request", () => {
    const keys = new Set(Array.from({ length: 25 }, createYouDubOperationKey));
    expect(keys.size).toBe(25);
    expect([...keys].every((key) => key.length <= 255)).toBe(true);
  });
});
