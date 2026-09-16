import { describe, expect, it } from "vitest";

import { ffplayoutUrlForLocation, toWebSocketUrl } from "./urls";

describe("toWebSocketUrl", () => {
  it("converts HTTP and HTTPS orchestrator URLs", () => {
    expect(toWebSocketUrl("http://localhost:8000/ws/events")).toBe("ws://localhost:8000/ws/events");
    expect(toWebSocketUrl("https://video.example/api/ws/events")).toBe("wss://video.example/api/ws/events");
  });

  it("resolves the same-origin /api websocket proxy", () => {
    expect(toWebSocketUrl("/api/ws/events", "https://video.example")).toBe("wss://video.example/api/ws/events");
  });
});

describe("ffplayoutUrlForLocation", () => {
  it("keeps the current hostname and moves playout to its dedicated port", () => {
    expect(ffplayoutUrlForLocation({ protocol: "http:", hostname: "192.168.5.23" } as Location)).toBe(
      "http://192.168.5.23:3003",
    );
    expect(ffplayoutUrlForLocation({ protocol: "https:", hostname: "video.example.com" } as Location)).toBe(
      "https://video.example.com:3003",
    );
  });

  it("formats IPv6 hostnames and supports a custom port", () => {
    expect(ffplayoutUrlForLocation({ protocol: "http:", hostname: "2001:db8::5" } as Location, "3101")).toBe(
      "http://[2001:db8::5]:3101",
    );
  });
});
