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

已知差异：Python 手动清理默认枚举所有终态且没有额外防误删，Rust 增加手动启用、批次限额和跨系统引用保护。定时资源清理与更全面的历史资源差分、真实工作流验收仍未完成，不能计为完全生产等价。
