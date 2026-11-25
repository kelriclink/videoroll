# 原生远程渲染活跃执行协作取消（2026-11-25）

基线 `b5d31e5`。延伸原生渲染管理的安全取消能力，不改变 Python 兼容路径和原有数据库结构。

- 管理员可取消具有精确任务/工作流/inbox/ticket 关联、无已登记上传结果的 queued/running/uploading 原生执行。沿用 task -> workflow -> job -> execution 锁顺序，在同一事务内取消四层记录。有效租约仍禁止重新排队，完成、已登记上传结果、外部不确定阶段、历史 Python 记录继续受保护。
- 取消后旧 heartbeat/progress/log/complete/upload 不能通过活动租约校验，原有 Worker cancel 轮询明确返回取消原因。保留原 worker、fence 和尚未过期的 lease，不再续期。现有 fail 回调在取消状态下只认可原 Worker 和 fence，清除租约但不重试、不改取消原因；重复停止报告安全。
- 在 Worker 管理读数、原生 heartbeat 返回的节点读数和 claim 并发检查中，已取消但租约有效的执行仍占用一个槽位；原生 render-tick 有界清理已过期取消租约。资源回收在有效取消租约期间保持阻塞。
- 协作取消不是远程强制杀进程。实际 Worker 若未发送停止回调（包括网络中断），依靠原租约到期释放；未验证实际 Windows/Linux GPU 节点终止延迟、正在传输的大文件竞争、通用任务停止 API 的租约收尾或资源 GC 专项。已登记上传的结果仍需独立恢复/处置策略。

验证：独立 target `/4t/videoroll-rust-render-active-cancel-target`、临时 PostgreSQL 端口 35473，`VIDEOROLL_V2_TEST_RENDER_ADMIN_ONLY=true` 和 `VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true` 两次专项 smoke 均退出 0。真实 HTTP/PG 断言包括三路并发取消、唯一取消事件、取消轮询、旧心跳和完成回调拒绝、错误 fence 拒绝、取消后槽位保留/拒绝新领取、正确停止确认和重复确认、离线租约过期清除、无后续配音或投稿阶段、历史 Python 与已上传结果保护；上一轮真实 MP4 上传后恢复和重试预算专项回归通过。Rustfmt 和 Git 差异检查通过。未运行全量接口差分或生产验收。
