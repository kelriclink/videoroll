import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRemotePayload,
  endpointPermissionPattern,
  normalizeRemoteEndpoint,
  normalizeYouTubeVideoUrl,
  resolveYouTubeVideoUrl,
} from "./shared.js";

test("normalizes supported YouTube video URLs", () => {
  assert.equal(normalizeYouTubeVideoUrl("https://youtu.be/dQw4w9WgXcQ?t=10"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(
    normalizeYouTubeVideoUrl("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share"),
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  );
  assert.equal(normalizeYouTubeVideoUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=demo"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(normalizeYouTubeVideoUrl("https://www.youtube.com/@creator"), null);
});

test("prefers the right-click link before the current page", () => {
  assert.equal(
    resolveYouTubeVideoUrl(
      {
        linkUrl: "https://youtu.be/BBBBBBBBBBB",
        pageUrl: "https://www.youtube.com/watch?v=AAAAAAAAAAA",
      },
      "https://www.youtube.com/watch?v=CCCCCCCCCCC",
    ),
    "https://www.youtube.com/watch?v=BBBBBBBBBBB",
  );
});

test("builds the remote endpoint and permission pattern", () => {
  assert.equal(
    normalizeRemoteEndpoint("http://192.168.1.9:3001"),
    "http://192.168.1.9:3001/api/remote/auto/youtube",
  );
  assert.equal(
    normalizeRemoteEndpoint("https://video.example.com/base/api/"),
    "https://video.example.com/base/api/remote/auto/youtube",
  );
  assert.equal(endpointPermissionPattern("http://192.168.1.9:3001/api/remote/auto/youtube"), "http://192.168.1.9/*");
});

test("inherits auto publish unless explicitly overridden", () => {
  assert.deepEqual(buildRemotePayload("https://www.youtube.com/watch?v=AAAAAAAAAAA", { license: "authorized", autoPublishMode: "inherit" }), {
    url: "https://www.youtube.com/watch?v=AAAAAAAAAAA",
    license: "authorized",
  });
  assert.deepEqual(buildRemotePayload("https://www.youtube.com/watch?v=AAAAAAAAAAA", { license: "own", autoPublishMode: "enabled" }), {
    url: "https://www.youtube.com/watch?v=AAAAAAAAAAA",
    license: "own",
    auto_publish: true,
  });
});
