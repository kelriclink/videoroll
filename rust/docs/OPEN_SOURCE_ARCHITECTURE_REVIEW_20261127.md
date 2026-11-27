# 第二十七批：成熟开源项目对照与全仓复查（2026-11-27）

基线 9b3658c。重新执行 audit-python-behavior.py --all-python：1208 个 tracked Python 文件、239148 行、10610 个定义、0 AST 错误；201 第一方业务、15 运维/模型桥接、14 历史迁移、240 测试/审计、738 第三方快照。rust/src 下 143 个 tracked .rs 文件、69471 行（修改前）。此为全量索引而非逐行语义签收。现有 PARITY_VERIFY_20261125_R9 记录 217 partial native、0 完整行为签收；本轮不虚增完成数。重点阅读 Python orchestrator_api/infrastructure/scheduler.py、maintenance_service.py 的原定时清理与投稿超时机制，以及 Rust scheduler.rs、main.rs、maintenance_publish_timeout_v1.rs、database.rs、media/asr.rs、agent_provider_cache.rs、storage.rs 等对应实现与已有迁移报告。外部研究基于官方仓库文档、公开源码页与发布说明，不声称完整克隆、运行或审计外部项目。

## 外部研究与可迁移设计

|项目/来源|证据与可用思路|VideoRoll 后续对照|
|---|---|---|
|[Graphile Worker cron](https://worker.graphile.org/docs/cron)|数据库协调分布式定时调度，支持补偿漏调度|main.rs 的多个周期维护循环；先做扫描 single-flight，后续持久化 due/cursor|
|[pg-boss queue policies](https://github.com/timgit/pg-boss/blob/master/docs/api/queues.md)|singletonKey、分组并发、dead letter、LISTEN/NOTIFY 唤醒及轮询回退|scheduler.rs、sau_worker_native_v1.rs、telemetry.rs；账号并发、区分 runnable/blocked/unknown、减少领取等待；不可重放的投稿不走通用 redrive|
|[Temporal Activity Definition](https://docs.temporal.io/activity-definition)|稳定 activity 身份、可重试活动必须幂等|operation_inbox 的原 operation key 和 generation/fence，严格区分未知外部效果|
|[Postiz releases](https://github.com/gitroomhq/postiz-app/releases)、[architecture](https://github.com/gitroomhq/postiz-docs/blob/main/self-host/architecture.mdx)|pending-post 防重复、媒体流式传输、Temporal 异步投稿|publish_jobs、social_publish.rs、sau_account_native_v1.rs；按平台/账号/目标核对外部回执，凭据版本隔离，不重新引入 Temporal|
|[Immich integrity](https://github.com/immich-app/immich/blob/main/docs/docs/administration/system-integrity.md)、[integrity.service.ts](https://github.com/immich-app/immich/blob/main/server/src/services/integrity.service.ts)|未登记文件、缺失文件、校验和不一致三类；checksum 时间/进度预算|storage.rs、storage_objects/refs、assets、render_executions；只读巡检、分类、分批 hash、禁止误删，后续显式回收|
|[tus protocol](https://github.com/tus/tus-resumable-upload-protocol/blob/main/protocol.md)、[tusd handler](https://github.com/tus/tusd/blob/main/pkg/handler/unrouted_handler.go)、[yt-dlp](https://github.com/yt-dlp/yt-dlp)|offset、expiration、上传锁、分片续传|download_runtime.rs、remote_render、storage.rs；临时文件身份和断点保护，旧 Python .partial 默认不自动删除|
|[VideoLingo technical](https://github.com/Huanshere/VideoLingo/blob/main/docs/pages/docs/tech.en-US.md)、[releases](https://github.com/Huanshere/VideoLingo/releases)|NLP+语义切分、独立阶段、字形宽度排版|media/asr.rs、readability、translation_context；已有展示 cue 与对齐 segment 分离，后续实际字体排版对比|
|[pyVideoTrans architecture](https://github.com/jianchang512/pyvideotrans/blob/main/docs/architecture.md)、[WhisperX](https://github.com/m-bain/whisperX)|可跳过阶段、音画对齐、可选二次 ASR、词级强制对齐及说话人识别|media/dubbing.rs、dubbing_handoff、asr_timing；保持 YouDub 为依赖，建立词时间轴/配音后字幕质量评测及缓存输入签名|

保留 Rust + PostgreSQL 原业务表、原 API 和第三方模型服务。不是建议复制其他项目全部代码或替换整个调度器。后续评估实际许可证和接口，不直接复制第三方代码。

## 本轮实际改动：投稿超时维护扫描跨实例 single-flight

Python 原版 scheduler 在存储清理循环内进行投稿超时检查。Rust 每个 Worker 独立 60s 扫描，CLI 同样直接 tick；之前隔离并发验证常见 publish_jobs 表锁竞争。参考 Graphile Worker 跨实例协调原则及项目既有 ASR 的 PostgreSQL session gate 实现。

- 以 videoroll:v2:publish-timeout-scan 为独立 advisory lock key，用专属非池化 PostgreSQL session 非阻塞领取；竞争失败立即返回 0，不移动本地 cursor，不执行候选扫描。
- 扫描会话连接超时 5s，禁 idle_session_timeout，避免长扫描时被正常空闲会话超时释放；每个候选处理前 ping 检测连接中断。会话 drop/断开由数据库释放锁，不把持锁连接还到 pool。每个任务仍独立事务、Task 行锁与原有 NOWAIT 表锁，scan gate 仅优化竞争，不是业务正确性依据。
- 不增加数据库表、依赖或 API；每个获得资格的扫描最多 100 条。并非持久化每分钟 exactly-once 调度；本地 cursor 仍可能在切换实例后重新扫描，外部写者仍可竞争；数据库连接中断瞬间可能存在一个正在执行的原事务，继续由原业务锁保护。新增每轮专用数据库连接和每候选一次 ping 成本，未来需生产规模压测。
- 专项增加独立数据库会话占用 gate，3 个 CLI 同时尝试均不得改变任务；会话关闭后恢复，确认未泄漏 gate。其余投稿超时/自动批次/终态批次回归继续覆盖。

## 后续路线（未实施）

P0：独立 UNKNOWN 回执核实（无外部可证实成功不允许重发）；多批次/账号级 currentness 与失败目标独立恢复；真实平台与生产配置隔离验收。P1：SAU 账号级领取及凭据版本、防旧会话覆盖；Immich 风格只读存储一致性巡检、分页预算、修复建议；分阶段任务队列观测与退避；保留可恢复临时文件；VideoLingo/pyVideoTrans/WhisperX 对齐与排版对比。P2：持久调度进度与任务级细粒度锁、指标基线及压力测试。后续不把框架名或静态源码扫描等同于实现与验收。
