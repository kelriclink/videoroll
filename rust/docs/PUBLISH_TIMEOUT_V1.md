# 原版投稿任务超时恢复（2026-11-27）

参考 Python `maintenance_service.expire_stale_publishing_tasks` 和 scheduler 的定时维护。Rust Worker 独立每分钟检查一次，沿用 `PUBLISHING_TASK_TIMEOUT_HOURS`（默认 48，限制 1..87600 小时）。`publish-timeout-tick` 可执行一页，不会联系平台或启动投稿。

- 每页最多 100 条 Rust 所有权且超时的 publishing 任务；Worker 保留内存 keyset 游标，到末尾回绕。进程重启从头扫描。
- Task 行 FOR UPDATE SKIP LOCKED；短暂非阻塞表锁保护原 publish_jobs、publish_batches、operation_inbox、outbox_events，避免在重新检查期间有旧执行者写入回执。
- 保留 Python 所有权、历史锁、活跃或不确定工作流、未结算 Inbox/Outbox、近期 job/heartbeat 或有效租约、已存在外部 ID/URL 的任务。批次投稿、SAU intent、P2 append 由独立机制负责，本模块不修改这些记录。
- 无外部投稿痕迹或仅明确失败回执：原任务状态 failed，error_code PUBLISHING_TIMEOUT。
- 遗留 draft/submitting 且无活动持有者：转 unknown，保留已有 response_json 与上传进度，释放旧租约，不再发送外部请求。已有 unknown 保持原回执；任务 UI 为 failed / PUBLISH_OUTCOME_UNKNOWN，不能作为普通失败重跑。
- 原业务 publish_jobs 中 draft/submitting/submitted/published/unknown 或任何外部 ID/URL 会阻止普通 workflow start/restart_at；显式原投稿处理器继续采用各自目标级回执策略。
- 不自动宣称投稿成功，不自动重放，不新增数据库业务表。独立原版 unknown 回执仍需人工平台核验，完整历史批次修复和独立回执确认入口尚待进一步验收。

隔离测试：`VIDEOROLL_V2_TEST_PUBLISH_TIMEOUT_ONLY=true bash rust/scripts/smoke-isolated.sh`。临时 PostgreSQL + Rust API/CLI，未运行执行投稿的 Worker。
