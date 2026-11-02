# ffplayout upstream in VideoRoll

- Upstream: https://github.com/ffplayout/ffplayout
- Imported release: v2.7.1
- Upstream commit: 2068b90d45c6819f8f819777b236890cfeb76f50 (2026-10-28)
- Imported source: vendor snapshot (not a git submodule)

VideoRoll-specific compatibility changes:
- `/playout/` reverse proxy prefix when building embedded Vue frontend (`vite build --base=/playout/`).
- Channel loopback HLS preview URL normalized to same-origin `/public/...` paths.
- Native upload size increased to 128 GiB.
- VideoRoll RTMP operations alert bridge ported into crates/app output driver; verify alerts with test mode and live diagnostics.

Never replace the running ffplayout binary while its service is active; preserve
and back up ffplayout SQLite before testing migrations. The native release
builder must use the current upstream `crates/app` and `crates/engine` layout.
