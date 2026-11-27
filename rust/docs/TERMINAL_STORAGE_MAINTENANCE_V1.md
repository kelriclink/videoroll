# Rust 原版终态任务资源清理（2026-11-24）

新增 POST /maintenance/storage/cleanup-terminal 原生处理器，使用原 PostgreSQL tasks、assets、subtitles、app_settings 和 storage_objects 相关引用表，没有新增影子业务表。响应使用 Python 原 StorageResourceCleanupRead 七个计数字段。

与 Python 原版对应：
- 终态 published、failed 或 canceled 且 stopped_status IS NULL；恢复中的取消任务不可删除。
- 删除七类原版资源命名空间 raw、final、sub、work、log、meta、dub 下属于这些任务的孤儿文件，并根据 assets.storage_key 和 subtitles.storage_key 删除仍引用的文件。
- 保留任务和运行历史；移除对应 assets、subtitles 行。文件删除遇到 IO 错误，会写入与 Python asset_service.queue_pending_storage_delete 同名、同哈希、同 JSON 字段的 app_settings 待重试项。
- 与 Python 使用相同 orchestrator.storage_resource_cleanup 锁键和租约过期时间，避免并发执行多个清理器。

Rust 额外的防误删规则：
- 必须显式启用 VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP=true；默认拒绝删除。
- 保留有活动字幕/渲染/配音、暂停或待确认 YouDub、原版 Inbox/投稿或 Rust 工作流进行中的任务。
- 保留存在共享 storage_key、storage_object_refs、播控链接、RenderWorker 引用的任务。
- 拒绝含符号链接的父路径；异常目录、可疑路径、超过 1,000 文件的任务保留元数据。
- 单次最多处理 32 个有资源任务，按 (updated_at,id) 分页，最多扫描 20,000 个终态任务，避免被历史空任务饿死。
- 清理期间锁定 Task 行。文件删除失败时写原 pending_delete durable 记录。

隔离测试由 VIDEOROLL_V2_TEST_TERMINAL_ONLY=true bash rust/scripts/smoke-isolated.sh 执行，
使用临时 PostgreSQL、真正 Rust API 和临时文件系统；覆盖终态、可恢复取消、运行中配音、共享资源、孤儿文件、符号链接、跨版本维护锁、IO 删除失败及 140 个历史空任务后的分页。
没有修改生产数据，也没有执行真实平台调用。

## 历史待删除队列接管（2026-11-27）

Rust Worker 的五分钟维护周期和 `gc --limit 100` 同时接管原 `storage.pending_delete.*` 记录，无需新增业务表。只有确认 Python 保留期维护线程已退出后，才启用已有的 `VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP=true`。默认不执行历史文件删除。

- 校验原 Python 哈希键、文件系统 bucket、请求时间和任务 UUID 命名空间；不处理 v2 对象。
- 锁定终态任务和待删除记录，检查任务断点、运行中的工作与所有资产及注册表引用。短暂非阻塞 SHARE 表锁保护跨任务引用；遇到竞争延后，不等待写事务。
- 已不存在的文件视为幂等完成；新于请求时间的文件、目录、符号链接、仍被引用的文件、无可信任务归属的记录一律保留。
- 每轮最多 200 条，按 updated_at 排序；延后记录至少五分钟后再次进入队列。保留原 JSON 内容，异常记录留待人工核查。
- 本策略比 Python 原版更保守：不会自动删除活跃任务、未知归属或非标准命名空间中的待删除文件；也不会自动清理 Python .partial 目录。

已知差异：Python 手动清理默认枚举所有终态且没有额外防误删，Rust 增加手动启用、批次限额和跨系统引用保护。历史资源差分、真实工作流验收仍未完成，不能计为完全生产等价。

## 定时历史存储保留期回收

须确认原 Python 保留期线程已停用，再同时启用 `VIDEOROLL_V2_ALLOW_V1_TERMINAL_CLEANUP=true` 和 `VIDEOROLL_V2_ALLOW_V1_SCHEDULED_CLEANUP=true`。第二个开关默认关闭，不因曾经启用手动清理而意外触发定时删除。

- 独立 Worker 维护循环，每分钟检查一次持久化截止时间，实际扫描周期复用 `STORAGE_CLEANUP_INTERVAL_SECONDS`（默认 3600，60–86400 秒）。
- 复用原 `orchestrator.storage_resource_cleanup` 锁，在原 JSON 内保存 `rust_scan_after_at`、`rust_scan_after_id`、`rust_scan_next_due_at`，每轮最多 128 候选任务、32 个实际回收任务。游标到末尾后回绕；进程重启不会重新从首批扫描。独立 `legacy-storage-tick` 命令允许显式触发单页，但不绕过开关或任务保留期。
- 失败任务使用原环境变量（默认 48h）；已投稿和不可恢复的取消任务使用原 `storage.retention.asset_ttl_days`；实际删除前持锁重新检查当前设置、任务状态、发布批次和其他引用。
- 只清理原七类任务 UUID 目录及对应资产行，不碰 v2 注册表、非标准路径、待删除队列已有记录、新于任务最后更新时间的文件、符号链接或有活跃引用的资源。
- 旧资源删除失败时保留原资产/字幕行，后续扫描重试。没有文件系统与 PostgreSQL 的跨系统原子事务；崩溃后再次扫描可处理缺失文件对应的旧行。
- 与原 Python 全盘扫描相比，故意保留非标准路径及近期被替换的文件，需要人工核查。
