# SAU 自有业务执行器 Rust 化

新模式直接使用原 PostgreSQL publish_jobs、outbox_events、operation_inbox，
不需要 Celery/Redis 传递 SAU 投稿任务，也不新增业务表。保留原 SAU CLI
及其 Playwright 浏览器运行时，Rust 负责持久入队、领取、租约、参数及媒体
归属验证、执行、回执、批次状态、资源保留及故障恢复。

## 执行与切换边界

- videoroll-sau 进程显式开启 VIDEOROLL_V2_SAU_ENABLE_NATIVE_CHECK=true 和
  VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH=true；需要登录时同时开启
  VIDEOROLL_V2_SAU_ENABLE_NATIVE_LOGIN=true。
- 原 VIDEOROLL_V2_SAU_ENABLE_BROWSER_OUTBOX 必须关闭（false）。两种投稿
  消费模式互斥。应用 Rust DB migration 000037 后再启用。
- 保留第三方 SAU CLI、浏览器、必要的 X11/noVNC 和共享媒体目录；配置
  SAU_EXECUTABLE、SAU_RUNTIME_DIR、SAU_COOKIES_DIR、STORAGE_ROOT
  （或 VIDEOROLL_V2_STORAGE_ROOT）、SAU_UPLOAD_TIMEOUT_SECONDS、
  SOCIAL_LOGIN_DISPLAY。编排层继续使用现有内部 HMAC SAU HTTP 接口。
- 原生入队限定 runtime_owner=rust 且 source_license 不为 unknown 的任务。
  旧 Python 任务所有权需另行停机迁移。不得直接把历史 dispatched/processing
  事件当作可安全重试的任务。上线前排空或核实旧队列，停止旧 SAU Worker、
  登录/检测服务和旧 SAU 生产者，核查是否有遗留浏览器子进程。不要同时启用
  两套运行时。新 native_pending/native_running/native_completed 不属于
  Python Dispatcher 可领取的 pending/failed/dispatching 状态集合。
- 一个服务进程内登录、检查、投稿串行执行；DB 事务锁、原任务/账号锁、
  唯一事件和作业租约负责防重，原浏览器目录的独占锁继续生效。
- CLI 不接收 VideoRoll DB/API 密钥，不保存原始输出。超时/异常非零退出
  在执行边界之后记 unknown；租约过期恢复也不重放。异常退出可能留下
  Cookie 文件，必须先核实并清理，不自动覆盖。
- 退出码 0 沿用原 publish_jobs.submitted，但 receipt_verified=false。
  编排层没有独立稿件 ID/URL 时收敛为 unknown；原批次不误标已发布，
  不产生旧 Python 工作流完成/清理事件。Rust GC 和手动终态清理保护
  未核实投稿。真实平台回执获取和完整批次确认后续流程仍需验收，
  不能把 CLI 成功等同于平台发布成功。

## 隔离验收

VIDEOROLL_V2_TEST_SAU_NATIVE_WORKER_ONLY=true bash rust/scripts/smoke-isolated.sh

使用独立 PostgreSQL、生产一致的状态 enum、全套 Rust 迁移和无网络模拟 CLI；
不启动 Redis/Celery，不发布真实稿件。验证 6 路防重、旧 dispatcher
隔离、三平台 CLI、预检查失败、非零退出/超时 unknown、强制终止恢复、
未核实批次无 Python follow-up、编排回执收敛。实际生产切换未执行。

## 尚存的自有 Python 调度边界

本轮替换 SAU 投稿 Worker 及 SAU Outbox 投递，而不是整个项目的 Celery
Dispatcher。B 站旧批次完成/清理事件、渲染控制中的旧 outbox 分支、
历史任务恢复/切换及真实平台端到端验收仍需要独立处理。
