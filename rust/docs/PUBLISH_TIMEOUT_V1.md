# 原版投稿任务超时恢复（2026-11-27）

参考 Python `maintenance_service.expire_stale_publishing_tasks` 和 scheduler 的定时维护。Rust Worker 独立每分钟检查一次，沿用 `PUBLISHING_TASK_TIMEOUT_HOURS`（默认 48，限制 1..87600 小时）。`publish-timeout-tick` 可执行一页，不会联系平台或启动投稿。

- 每页最多 100 条 Rust 所有权且超时的 publishing 任务；Worker 保留内存 keyset 游标，到末尾回绕。进程重启从头扫描。
- Task 行 FOR UPDATE SKIP LOCKED；短暂非阻塞表锁保护原 publish_jobs、publish_batches、operation_inbox、outbox_events，避免在重新检查期间有旧执行者写入回执。
- 保留 Python 所有权、历史锁、活跃或不确定工作流、未结算 Inbox/Outbox、近期 job/heartbeat 或有效租约、已存在外部 ID/URL 的任务。SAU intent、P2 append 由独立机制负责；批次投稿的额外保守恢复条件见第二十四批。
- 无外部投稿痕迹或仅明确失败回执：原任务状态 failed，error_code PUBLISHING_TIMEOUT。
- 遗留 draft/submitting 且无活动持有者：转 unknown，保留已有 response_json 与上传进度，释放旧租约，不再发送外部请求。已有 unknown 保持原回执；任务 UI 为 failed / PUBLISH_OUTCOME_UNKNOWN，不能作为普通失败重跑。
- 原业务 publish_jobs 中 draft/submitting/submitted/published/unknown 或任何外部 ID/URL 会阻止普通 workflow start/restart_at；显式原投稿处理器继续采用各自目标级回执策略。
- 不自动宣称投稿成功，不自动重放，不新增数据库业务表。独立原版 unknown 回执仍需人工平台核验，多批次、自动批次父工作流与独立回执确认入口尚待进一步验收。

隔离测试：`VIDEOROLL_V2_TEST_PUBLISH_TIMEOUT_ONLY=true bash rust/scripts/smoke-isolated.sh`。临时 PostgreSQL + Rust API/CLI，未运行执行投稿的 Worker。

## 第二十四批：原批次聚合与超时回执保全

基线 deecbf6。对照 Python publish_lifecycle.py 的 recover_stale_publish_dispatches、evaluate_publish_batch、evaluate_task_publish_batches 和 Task→Batch 锁顺序，以及 maintenance_service.py 的超时规则、social_publisher/worker.py 的恢复入口。原 Python 仅凭无活动作业就可能把整个部分成功任务标记失败；不能直接照搬。原 Rust 20 批完全跳过带批次的超时任务。

- 仅处理 Rust 所有权、单个明确 rust-mixed-v1 手动批次（active，任务/批次/所有 job 均超过配置截止时间，未结束、无 cleanup 标记、无 auto_parent、任务 active_publish_batch_id 精确匹配）。每个预期平台/账户必须有且只有一个同任务/批次 job，无额外 job 或 outcomes_json。Task 行锁及 publish/inbox/outbox 非阻塞表锁下复核。
- 保留已有 published/submitted 且有平台 receipt ID/URL 的目标；原 Rust SAU 的 submitted/receipt_verified=false 且无外部标识仍按 unknown 聚合，不覆写原记录。无外部 ID 的 failed、既有 unknown 保留。仅隔离完全缺失关联投递账本且 operation_key 精确匹配的旧 draft/submitting 孤儿目标为 unknown；保留 JSON、上传进度，不创建 dispatch。任何现存该目标投递账本、其他未结算操作、活跃工作流、有效或残存租约、外部标识未决、历史多批次/缺目标/重复目标/跨任务关联均保守推迟。
- 复用正常批次 reconciler，全部成功才能生成原生 finished 与至少 24h 后 cleanup 事件；部分失败保持 publishing / PUBLISH_PARTIAL_FAILURE，不生成 cleanup；全部失败按原聚合 failed。UNKNOWN 和原生未核实 SAU 仍受既有存储及普通 workflow 重启栅栏保护。原生 finished 事件不是投稿操作。不会自动重试目标。
- Bilibili mark_uncertain 在原任务及平台范围内更新，保留原 response_json（冲突字段通过 previous_response 保存）、原 progress 和原 operation result（previous_result）；新回执明确 confirmed=false。仅当原 job 确实转 unknown 才结算原 Python fence，且要求 lease_owner=rust-v2，避免覆盖另一持有者或已结算结果。
- 本批不处理历史 Python 所有权、自动批次父工作流、多个当前渠道批次或有投递记录但证据冲突的目标；这些仍需针对账本状态的显式核对。不是完整生产接管。没有新增表或生产写入。
