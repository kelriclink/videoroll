# Rust 原生 SAU 独立投稿入口

新增 POST /sau/{platform}/publish，沿用 Python 服务的 HMAC 认证、请求与响应字段，以及原 accounts、tasks、assets、publish_batches、publish_jobs、outbox_events。

该入口负责 Rust 原生投稿接收、参数检查与事务性持久化，不转发至 Python HTTP 投稿接口。旧兼容模式仍使用原 Outbox Dispatcher；新的原生执行模式直接由 Rust 消费，详见 SAU_WORKER_NATIVE_V1.md。两种执行模式必须互斥，默认拒绝投稿。

- 校验平台、账号 active/valid 状态、批次目标、任务媒体归属和存储路径；严格要求视频及可选封面登记为该任务的原 assets。旧版未登记封面需要补登记，不静默访问其他任务文件。
- 按 Python 原版常见元数据输入规范处理抖音作者、熟肉前缀、标签和小红书数量限制。尚未逐项验收 Python 任意类型字符串转换及全部 Unicode casefold 边界。
- 使用任务、账号行锁与原 operation_key，投稿记录、待投递事件及非批次任务状态同一事务提交。既有 Outbox 事件不重置或重复创建。
- 保留既有 submitting/unknown/submitted/published 回执；成功回执可以复制给新批次而不再发布。未知回执禁止 force_retry；其他批次已有未决回执时拒绝新投稿。
- 仅当既有 submitting 记录无开始时间、无租约时才补齐缺失的投递记录；独立 Worker 的实际外部执行及调度恢复仍需生产级测试。
- 本模块最初完成时矩阵为 212/217；当前独立 SAU 账号与登录 API 已补齐，仍为部分适配，真实执行链需独立验收。

隔离验收命令（临时 DB、无真实投稿）：
VIDEOROLL_V2_TEST_SAU_NATIVE_PUBLISH_ONLY=true bash rust/scripts/smoke-isolated.sh

测试验证三平台、Python 元数据及 outbox 消息格式、8 路并发同一 job、重启回执恢复、未知禁止重发、批次历史结果复用、强制 outbox 故障事务回滚、认证与媒体归属。不得将此测试视为真实浏览器、生产账号或真实平台验收。
