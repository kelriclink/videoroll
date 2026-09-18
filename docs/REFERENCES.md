# External reference projects

VideoRoll does not keep full snapshots of unrelated upstream repositories in the main Git tree. Reference code should be inspected from its upstream repository (or a temporary developer checkout) and must not become a runtime dependency by accident.

| Project | Upstream | How VideoRoll uses it |
|---|---|---|
| biliup | https://github.com/biliup/biliup | Behavioral/reference material for Bilibili upload flows. VideoRoll has its own publisher implementation and does not import biliup at runtime. |
| bilibili-API-collect | https://github.com/SocialSisterYi/bilibili-API-collect | API documentation/reference material only. The upstream repository is archived; no source from it is required at runtime. |
| social-auto-upload | repository configured by `.gitmodules` | Runtime/build dependency for the isolated social-publisher image; kept as a pinned Git submodule. |
| ffplayout | source under `services/ffplayout/` with `UPSTREAM.md` | Integrated playout engine with local VideoRoll build/integration changes. |

When behavior is derived from an upstream project, prefer a short source comment or a link to the relevant upstream file/issue instead of copying the whole repository into VideoRoll.
