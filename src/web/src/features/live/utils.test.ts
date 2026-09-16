import { describe, expect, it } from "vitest";
import { buildLiveAudioControlPayload, moveItem } from "./utils";

describe("moveItem", () => {
  it("moves an item without changing out-of-range lists", () => {
    const items = [{ source: "library" as const, id: "a" }, { source: "library" as const, id: "b" }];
    expect(moveItem(items, 0, 1).map((item) => item.id)).toEqual(["b", "a"]);
    expect(moveItem(items, 0, -1)).toBe(items);
  });
});

describe("buildLiveAudioControlPayload", () => {
  const queue = [{ source: "library" as const, id: "song-1" }, { source: "library" as const, id: "song-2" }];

  it("sends the full visible queue when playing a selected song", () => {
    expect(buildLiveAudioControlPayload({ action: "play", audioItem: queue[1], queue, playbackMode: "shuffle", volumePercent: 100 })).toEqual({
      action: "play", audio_item: queue[1], audio_items: queue, playback_mode: "shuffle",
    });
  });

  it("bounds seek and volume payloads", () => {
    expect(buildLiveAudioControlPayload({ action: "seek", audioItem: null, queue, playbackMode: "sequential", positionSeconds: -4, volumePercent: 100 })).toEqual({ action: "seek", position_seconds: 0 });
    expect(buildLiveAudioControlPayload({ action: "set_volume", audioItem: null, queue, playbackMode: "sequential", setting: 250, volumePercent: 100 })).toEqual({ action: "set_volume", volume_percent: 200 });
  });
});
