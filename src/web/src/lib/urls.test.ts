import { describe, expect, it } from "vitest";

import { toWebSocketUrl } from "./urls";

describe("toWebSocketUrl", () => {
  it("converts HTTP and HTTPS orchestrator URLs", () => {
    expect(toWebSocketUrl("http://localhost:8000/ws/events")).toBe("ws://localhost:8000/ws/events");
    expect(toWebSocketUrl("https://video.example/api/ws/events")).toBe("wss://video.example/api/ws/events");
  });

  it("resolves the same-origin /api websocket proxy", () => {
    expect(toWebSocketUrl("/api/ws/events", "https://video.example")).toBe("wss://video.example/api/ws/events");
  });
});
