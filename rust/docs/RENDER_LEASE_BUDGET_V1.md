# Rust 原生远程渲染租约重试预算（2026-11-25）

基线 `b0df119`。远程 Worker 的显式失败回调原有五次领取上限，但 Worker 断线/崩溃时过期租约可以无限重新领取，且原回执扫描不处理预算耗尽的活动执行。

- 领取 SQL 和显式失败回调共用 MAX_RENDER_ATTEMPTS=5；预算耗尽的执行不能第六次领取。
- render-tick 在原物理 render_executions 按精确关联的 Rust inbox、工作流、任务所有权及 external_ticket 选择耗尽预算的 queued 或已过期 running/uploading 执行；有界扫描，行锁 SKIP LOCKED，先持久化 failed、错误及完成时间并清除租约，再走原失败回执结算路径。
- 有效租约、预算未耗尽、暂停工作流、错误票据不被扫描处置；不更改历史 Python 执行，不删除已存在资产，不创建新表或改变旧 API。
- 若在持久化失败与工作流结算之间中断，既有失败回执扫描可恢复。并发重复结算不产生第二条 render.remote_failed 事件。管理员手动重排及历史 P2 重新入队仍不在本轮范围。

验证：独立 worktree 与 target `/4t/videoroll-rust-render-lease-budget-target`，使用隔离 PG 端口 35470，运行 `VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh`，退出 0。原历史 P2 取消、回执恢复、真实 HTTP 回调幂等及新增耗尽预算专项通过；三路并发 tick 后核对失败状态、租约、完成时间、工作流和 inbox 状态及唯一失败事件。错误资产关联夹具仍被明确拒绝，不伪造完成。契约矩阵仍 217/217 partial native；本轮未执行全量业务 smoke、真实远程设备、生产数据库或真实投稿。
