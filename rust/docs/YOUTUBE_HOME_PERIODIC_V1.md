# Rust YouTube 首页推荐流周期调度 — 2026-11-24

在 videoroll-v2 worker 中新增周期调度：
- Worker 启动后立即检查是否到期，之后每隔 60 秒检查。
- 只在原 app_settings 中的 youtube.settings.home_scan_enabled=true 且根据 home_scan_interval_minutes 到期时执行；不另建影子调度表，也不依赖 Hatchet。
- 启用或停用配置使用已有 PUT /settings/youtube；HTTP API 进程本身不启动后台扫描。
- 复用上轮原生 /sw.js_data + /youtubei/v1/browse、Shorts/时长筛选、原 ingested_videos 唯一索引和工作流队列。本轮不新增 API 路由，保持 187/217 部分映射。

并发和重启：
- Python 原版 home_scan_lock_owner/home_scan_lock_until 租约、心跳与完成统计维持不变。
- 到期判断与领取租约发生在同一个事务的 SELECT ... FOR UPDATE 中。未到期、配置缺失、禁用、已有别人扫描均不会写入。
- 到期算法保持 Python 原规则：home_scan_last_finished_at 优先于 home_scan_last_started_at；间隔 1–1440 分钟。手动强制 POST 不受禁用限制。
- 旧进程崩溃的租约直到过期才会被新 Worker 接管；重启 Worker 会重新计算到期时间。
- 扫描失败仍记录 home_scan_last_finished_at 和 home_scan_last_error，在下个配置间隔后再执行，避免重试风暴。
- 自动扫描是 Worker 内独立 tokio task，tick 使用 Skip 避免耗时较长时连续补跑。
- 修复前一轮手动扫描来源字段原子性：Scheduler INSERT Task 时分别绑定 origin_kind 与 created_by，在同一事务内写入来源和工作流；不再后置更新。
- 增加独立 videoroll-v2 home-tick 诊断命令：只执行一次与后台完全相同的到期检查，输出 home_scan_run=true/false，不执行 Worker 媒体队列，也不能绕过间隔或锁。

隔离测试：
VIDEOROLL_V2_TEST_HOME_ONLY=true bash rust/scripts/smoke-isolated.sh
覆盖临时 PostgreSQL、本地 YouTube Browse、手动扫描、禁用/空设置、间隔判定、Python 活动锁、过期租约恢复、两个 Rust 进程同时争抢和上游 503 后的失败重试时间。

没有连接真实 YouTube 登录推荐流，也没有投入生产。完整业务等价仍需生产负载验收。
