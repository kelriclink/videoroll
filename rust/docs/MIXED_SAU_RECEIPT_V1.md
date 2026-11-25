# 混合批次 SAU 人工回执核验（2026-11-25）

基线 `fd7eab7`。原 Python SAU worker 根据 CLI 退出码仅标记 submitted/unknown；检查本地第三方 SAU 的 Douyin、Xiaohongshu、Kuaishou uploader，未发现可供 VideoRoll 稳定绑定本次提交的结构化作品回执。因此本轮不解析成功日志冒充外部平台确认，也不访问平台或重放投稿。

新增受原认证中间件保护的 `POST /api/v1/tasks/{task_id}/publish_jobs/{job_id}/sau/resolve`。显式 native mixed 开关须启用。请求 JSON 为 `platform`、`account_id`、`external_id`（平台作品原始 ID，不是 URL）、`evidence`（12–2000 字符）及 `confirmed_in_creator_center: true`。这是已登录操作者在平台后台核对后的人工声明，不是程序独立核验。请勿在 evidence 输入 cookie、token 等敏感信息。响应 `verification=operator_creator_center`。当前仅支持本轮原生 mixed 批次，不修改历史 Python、无批次或纯 SAU facade 稿件。

Task -> Account -> Job -> Batch 事务顺序，要求 Rust 所有权、非 canceled、原活动批次指针、确切 task/job/platform/account、完整且唯一的批次目标、原 SAU Worker 的 completed inbox/native_completed outbox 与 started_at、无租约，且原状态 submitted/unknown、原响应 driver=sau/runtime=rust/receipt_verified=false。保留原 `operation_inbox`/`outbox_events` 执行回执；`publish_jobs.response_json` 追加 operator_receipt 与时间，保留 receipt_verified=false 区分自动验证。原稿件设置 published/external_id。相同证据幂等返回，不同证据拒绝覆盖，按平台和作品 ID 使用事务 advisory lock 拒绝跨稿件重复绑定；该锁约束本入口，不是所有外部写入者的全局唯一数据库约束。拒绝错误平台/账号、跨任务 ID、缺失执行记录、活跃租约、B 站稿件、已确认或冲突记录。不支持否定式确认、重新投稿或自动查询；作品 ID 格式校验是保守的本地校验，非平台认证。原认证为管理员会话/内部服务令牌，未引入独立操作者身份审计；无前端确认按钮，本轮仅后端接口。

原批次 reconciliation 在同一事务重新结算，未确认目标继续阻断完整成功及清理。批次由失败/部分失败转为 succeeded 时更新 finished_at，新的清理截止从成功时间计算，避免旧失败时间让清理提前。原成功通知/清理 Outbox 仍采用原幂等操作键，不发送 Celery 或 Hatchet 消息。没有新增数据库表或迁移。原 217 条接口不变；新扩展接口不计入旧覆盖数。

实际隔离验收：`BILI_DIRECT_ONLY=true MIXED_PUBLISH=true` 对应 smoke 环境变量执行退出 0，32 个 PASS 标记。真实 Rust API/两类 Rust Worker + loopback Bilibili + 无网络 SAU CLI，两个混合批次分别验证三次并发同证据核验仅一次成功事件/清理事件，原回执不覆盖，拒绝匿名、错误账号、跨任务、URL、未明确确认、重复作品 ID、不同说明；三目标批次第一个 SAU 核验后仍 partial_failed，最后一个核验后 succeeded，未知（浏览器启动后退出非零）可凭人工证据确认；原失败时间模拟为三天前，新的清理时间仍为未来 24 小时。核验前后 B 站 submit 及 SAU CLI 调用次数不变。另 `CONTRACT_217_ONLY` 退出 0：204 正向、12 拒绝、1 主动阻断、0 异常。cargo check --locked、fmt --check、py_compile、git diff --check 通过。日志 `/4t/videoroll-rust-mixed-receipt-target/MIXED_RECEIPT.log`、`CONTRACT_217.log`，隔离 PostgreSQL 35487。

仍待：独立平台 API/浏览器只读回执核验、前端安全确认 UI、纯 SAU 统一批次、历史记录核验与接管、账号轮换保护、生产配置/真实平台验收。未执行实际资源到期清理、完整 smoke 或前端构建；未真实登录、真实投稿、修改生产库、部署或推送。
