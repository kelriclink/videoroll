# 第十三批：提交后实时通知与队列执行事实

基线 d10a5f6。继续逐模块阅读原 Python，并移植可验证的业务行为。全仓 AST 清单不等于语义签收。

## 原 Python 对照

- src/videoroll/realtime.py:262–440、orchestrator_api/realtime.py、tests/test_realtime.py：Python ORM after_flush 收集、after_commit 发事件、after_rollback 清除；订阅、队列溢出、重连、资源采样。原 Rust 按 (created_at,UUID) 查询 workflow_events，可能漏掉晚提交的较早事务，且无法覆盖没有工作流的 CRUD 或删除。
- subtitle_service/queue_telemetry.py、subtitle_service/main.py、workflows/pipeline_status_sync.py：区分真实运行与等待，不以任务业务状态冒充执行状态；未知/截断指标不得假造。
- storage/recovery.py 全部阅读：原脚本在丢失资产时删除 PublishJob/Batch、RenderJob、SubtitleJob、Subtitle、Asset。不能直接复制，会丢历史回执及审计证据；后续设计保留历史与执行代数隔离的恢复流程。本轮没有改变源恢复或清理策略。

## 实现

- 新增第42条 additive migration，在原任务、资产、工作流、执行、投稿、账号、Agent 和相关设置表上创建提交后 PG NOTIFY 触发器；无新增业务表，通知仅路由标识，不传账号凭据、请求、结果、原始模型内容。纯 inbox/render 心跳不发通知。
- API 进程共享一条独立 LISTEN 连接、512 条 broadcast 缓冲，800ms 合并按订阅范围的 resync_required；首次订阅、监听连接重建、缓冲落后均刷新权威快照。任务删除额外发送 task.deleted。取消旧时间游标。此设计是快照一致性恢复，并非完整持久事件重放；更新事件多数改为 targeted invalidation，旧第三方消费者若强依赖逐条完整事件 DTO 需验证。
- PostgreSQL 原生队列按所有权、当前阶段、generation、租约、可执行时间、attempt budget 区分 Ready、Active、Awaiting、Delayed、Unknown receipt、Exhausted。Ready 是符合领取条件但尚未应用全局并发限制的数量。Unknown 仅指当前未结算的过期投稿阶段，不是全部历史待核对回执。保留原 unacked 数值字段并附 applicability=false；不伪造 Kombu Reserved。下载并发占用排除无效 native 代数。scheduler 固定 rust-v2，worker slots 使用配置总 worker_count，不冒充独立探测的实际活跃字幕 Worker 数量。
- 前端处理队列改为服务器推送 20s 快照，移除浏览器固定 broker API 轮询；有事件或重连时刷新业务快照。针对订阅 topic 触发 resync；增加队列/主页请求代数保护与任务 404 清除。兼容旧服务器无 topics 的全局 resync。其余 Dashboard/Agent 原有兜底刷新保留。

## 验收

- Rust final lib 261/261，原 Python realtime/pipeline status 19/19（2条依赖库弃用警告）；此前 RenderQueuePage 轮询静态断言已通过。
- 新的真实 PG18 + 已认证 WebSocket 专项实际通过：并发事务逆序提交、回滚、无工作流 CRUD、删除、pg_terminate_backend 强制重建 LISTEN、按订阅 resync、队列分桶及推送；隔离数据库使用完整 synthetic legacy bootstrap + 42 migrations。没有把默认跳过 env-gated 测试当作实测。
- React/Vitest 62/62，TypeScript/Vite build 通过（Node22）。
- 主服务 Debug 二进制重新构建；原生产 DDL schema-only 克隆应用全部迁移，七服务/真实 Nginx 启动通过。独立服务测试中的 Egress/SAU 可执行文件为既有产物（本轮不修改这两个独立程序）。Nginx 隔离测试存在 worker_connections 2048 > 文件描述符限制1024 的警告，不影响本轮断言；正式部署仍应确认 systemd LimitNOFILE。
- 日志 /tmp/vr-b13-final-lib.log、/tmp/vr-b13-realtime.log、/tmp/vr-b13-python.log、/tmp/vr-b13-web-final.log、/tmp/vr-b13-web-final-build.log、/tmp/vr-b13-schema.log。
- 尚未测试生产高并发 NOTIFY 吞吐、旧外部 WS 消费者事件兼容、全量 API 217复测、真实断网浏览器端自动重连视觉测试及最新正式 Release。未部署、未修改生产数据库、未真实投稿。

下一批 P0：安全源文件恢复、清理隔离及历史回执保留；P1：可信反代和社交投稿账号并发。
