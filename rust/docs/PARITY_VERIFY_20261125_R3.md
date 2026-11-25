# 2026-11-25 全接口隔离验收第三轮

基线 `10f9119`。只修改隔离测试及验收记录，不改变生产业务代码或数据库结构。新增独立 `contract_media_v3.py`，由原 217 路由探测调用；对已覆盖的同方法/原路径只统计一次。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 上轮独立探测 | 217 | 142 | 74 | 1 | 0 |
| 本轮媒体/字幕 | 217 | 151 | 65 | 1 | 0 |
| 本轮媒体/字幕/配音 | 217 | 154 | 62 | 1 | 0 |

最终 12 条新夹具按真实 HTTP 及数据库状态验证：视频 multipart 上传和 SHA-256/原字节，封面 PNG 解码尺寸，完整下载及 Range 206 字节，跨任务隔离，ffplayout 引用幂等，删除最终资产及原 playout 映射/引用并延迟 GC，手动字幕、字幕恢复、自动字幕交接，手动 YouDub 入队及幂等、暂停、失败重试保留原失败代次。每条均先执行无鉴权请求并验证拒绝。使用独立任务/真实媒体，不运行外部投稿或消费新增工作流。首次测试断言把视频上传预期写成 200，实际原契约 201；仅修正测试预期后通过。

本轮复跑现有 `WORKER_AUTH`、`RENDER_EVENTS`、`RENDER_ADMIN` 三组隔离专项，全部退出 0：旧/新 Worker 凭据、单次注册、跨身份保护、实际 MP4 回执、重试预算、暂停恢复与取消、错误 fence、重复回执、租约释放。没有将专项 PASS 行计入全量正向数字；Worker 协议逐路由独立正向夹具仍未接入全量探测（原计划大脚本未写入成功，改为独立专项）。

日志：`/4t/videoroll-rust-api-parity-media-target/contract-217-r3.log`、`contract-parity-217.json/.md`、`WORKER_AUTH.log`、`RENDER_EVENTS.log`、`RENDER_ADMIN.log`。独立 PostgreSQL 35478。未运行本轮完整 smoke、前端构建、真实 GPU/SAU/Bilibili 外部操作或生产配置。154 是正向请求数，不是 Python/Rust 全业务等价签收数；矩阵仍为 217 partial native、0 full parity signoffs。
