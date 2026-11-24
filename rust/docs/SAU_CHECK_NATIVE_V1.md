# SAU 原生账号导入与检测

新增两个独立服务兼容入口：

- POST /accounts/{platform}：原 multipart account_name + file，校验平台、账号名、1 MiB UTF-8 storage_state；保留原 account/check_job_id 响应。
- POST /accounts/{account_id}/check：原账号有效性与忙碌检查，异步检测请求。

原 PostgreSQL accounts 与 operation_inbox 为唯一持久化来源，新增迁移只创建三个局部索引。operation_inbox 使用独立 runtime_engine=rust-sau-check，避免被 Rust 工作流调度器当作媒体任务执行；请求中仅存账号 ID，不存 Cookie。原版 dbplain:v1: 为可恢复的数据库明文格式，并非加密。保留原凭据兼容，不宣称已经实现数据库静态加密。历史 Fernet 加密凭据需要按原版方式恢复后再导入，不能假装检测成功。

账号修改和检测入队使用同一事务、账号行锁以及 pending 操作唯一索引。并发重复导入和重新检测复用已排队的 ID；重新导入新凭据时旧 pending 操作标记 superseded。检测中或有 submitting 投稿的账号拒绝修改、检测和撤销。执行结束必须核对操作、账号状态、rotated_at 及原凭据；不会将旧结果覆盖已更新凭据。

执行组件：
- 在 videoroll-sau 进程配置 VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK=true 才启动检测 Worker；未启用时拒绝账号导入和检测。运行前应用 Rust 数据库迁移。
- SAU_EXECUTABLE（默认 sau）、SAU_RUNTIME_DIR、SAU_COOKIES_DIR 与原 CLI 配置一致，cookies 必须为 runtime 目录下的 cookies；已有目录要求严格。SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS 默认 120，允许 1..600 秒。
- 原生 Worker 直接启动第三方 SAU CLI check，使用 0600 临时 cookie 文件、独占目录锁、无 shell 参数拼接。输出不回传 CLI 原始日志，成功时校验更新后的 storage_state 后保存；失败保留原凭据，正常结束删除临时文件。
- 过期 processing 记录标记 error，禁止自动重复执行中断的浏览器进程。强制终止后可能遗留 Cookie 文件；拒绝覆盖该文件，需人工核实进程已退出并安全清理后重试。Linux 父进程死亡信号只能直接约束 CLI 主进程，仍需独立验证真实 Playwright 子进程生命周期。
- 同一账号不能同时由旧 Python 导入/登录/检测 Worker 与新 Rust 账号检查 Worker 管理。上线前停掉旧检查入队并排空旧检查任务；原 SAU 投稿 Worker/Outbox Dispatcher 当前仍是投稿执行链，需继续迁移或保留。真实登录会话三条 API 仍待迁移。

隔离测试：
VIDEOROLL_V2_TEST_SAU_NATIVE_CHECK_ONLY=true bash rust/scripts/smoke-isolated.sh

测试使用一次性 PostgreSQL 与模拟 SAU CLI（不接触真实平台/账号），覆盖原 DTO 和 storage_state 格式、6 路并发、重复检测、检查/投稿忙碌保护中的检查分支、凭据刷新、错误及超时、残留文件、进程强制终止与过期租约恢复、撤销后不恢复旧凭据。
