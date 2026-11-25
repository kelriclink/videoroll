# 2026-11-25 全接口隔离验收第二轮

基线 `82f2914`。本轮仅修订探测夹具与断言，未更改生产业务行为、接口或数据库结构。

| 场景 | 总接口 | 正向 | 仅拒绝 | 主动阻断 | 异常 |
|---|---:|---:|---:|---:|---:|
| 先前独立探测 | 217 | 128 | 88 | 1 | 0 |
| 本轮独立探测 | 217 | 142 | 74 | 1 | 0 |
| 先前完整 smoke | 217 | 125 | 91 | 1 | 0 |
| 本轮完整 smoke | 217 | 139 | 77 | 1 | 0 |

本轮两次运行均退出 0。完整 smoke 有 153 个 PASS 行，其中 54 个 PASS DIFF 行（不是独立 API 签收数量）。新增 8 个明确正向业务响应断言：渲染详情 ID/进度/指标、投稿元数据 GET/PUT、YouTube 缓存元数据、orchestrator SAU 会话 GET/DELETE、独立 SAU 会话 GET/DELETE。另补充原渲染 Worker/device/enrollment 的隔离夹具和有效更新体；SAU proxy 创建会话后读取，独立 SAU 读取及取消持久化会话不启动真实浏览器。

原生 P2 完整 mock 回归（先前未执行）通过：上传失败、显式重试、同请求去重、原稿件元数据、已确认上传回执的仅一次 edit、P1 原生字幕证据保持、GC 引用释放、HTTP 503 unknown 禁止重放。完整测试另覆盖旧任务停止/恢复、真实 FFmpeg 烧录及媒体、Windows RenderWorker wire 模拟、YouDub adapter、B 站字幕、SAU 三平台 mock、RAG、外部 ASR mock 与原版同夹具差分。初始 mock API 健康轮询的 curl connection refused 后续恢复，不是业务探测失败。

独立/完整探测结果不可相加。九条入口因原任务状态和资产夹具不同而出现 2xx/4xx 差别：资产下载/流、YouTube 下载进度、auto_subtitle_handoff、auto_youtube_start、youtube_download_enqueue、youtube_meta action、stop、resume。余下拒绝路径包括预期退役 410、鉴权/SSRF/无白名单代理拒绝、真实浏览器/平台及专用工作阶段需隔离夹具。真实 B 站 posting 仍主动阻断。不能把 2xx 探测或 PASS DIFF 数量折算为生产等价签收；矩阵仍为 217 partial native、0 full parity signoffs。后续应按原服务/路由追加正向/拒绝/状态副作用的 Python/Rust 明确证据，优先无独立正常夹具的 Worker wire、媒体、任务 stage 与独立 subtitle/Bilibili/SAU 路径。

日志与逐项报告：`/4t/videoroll-rust-api-parity-fixtures-target/contract-217.log`、`full-smoke.log`、`contract-parity-217-standalone.json/.md`、`contract-parity-217.json/.md`。独立 PostgreSQL 35477；临时容器/带隔离标记的卷及测试监听端口均已清理。未执行生产配置、真实平台账号、生产数据库写入或部署。未增加新数据库迁移；未运行本轮完整前端构建。
