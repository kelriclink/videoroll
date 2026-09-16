# ffplayout upstream

- Repository: https://github.com/ffplayout/ffplayout
- Branch: `main`
- Imported commit: `fdedbbf774cbac7306b1a065a1ec4fd63612e867`
- Imported at: 2026-09-16

This directory is kept as an independent ffplayout service. VideoRoll-specific
integration is provided by the parent Compose files and
`Dockerfile.videoroll`; the upstream Rust backend, Vue frontend, migrations,
assets, and tests are otherwise unchanged. The channel API has one small
integration patch: seeded loopback HLS preview URLs are returned as same-origin
`/public/...` paths when the service is behind VideoRoll's internal-only
listener, while explicitly configured URLs are preserved.
