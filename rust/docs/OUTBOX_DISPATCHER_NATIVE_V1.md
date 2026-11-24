# Rust 全局 Outbox Dispatcher

统一控制平面每 5 秒执行 outbox_dispatcher::tick，分别消费原生批次
通知/清理并恢复远程渲染回执。两条路径独立报错，不调用 Celery Broker。
SAU 长时间浏览器执行仍由独立 Rust SAU 进程负责，其 native_pending/
native_running/native_completed 队列不由 Python Dispatcher 领取。
B 站投稿/P2 与其他原生工作流阶段仍由原 Rust 持久执行器领取。
不新增表，不修改原 API。

## 运行与审计

- outbox-tick: 单次短事务分发及渲染回执恢复。
- outbox-audit: 只读 JSON 审计，按处理器/状态/处置类型统计原 outbox。
- outbox-audit --require-ready: 若存在未解决的投递记录，退出非零。
- VIDEOROLL_V2_NATIVE_OUTBOX_ONLY=true: 拒绝 legacy publish、
  compat write 和 SAU legacy browser outbox 模式。SAU 服务同样设置该变量，
  显式启用 native check/native publish；独立服务需要正确配置。
- 审计就绪只表示投递账本没有已识别的阻塞行，不代表真实平台已发布、
  历史工作流已迁移、第三方运行时可用或旧 Broker 已关闭。

## 历史切换（不是默认在线操作）

停止所有旧、新生产者与消费者，并核实历史投递及浏览器进程。备份数据库。
在审计后的数据库上使用 native-only 模式运行 outbox-adopt --quiesced。
确认 Rust SAU 执行器已配置且能消费此数据库时可追加 --sau-ready。
--quiesced/--sau-ready 是操作者确认，不是自动探测。
事务内 NOWAIT 表锁失败即回滚，不会等待并抢占活跃写入。
只接管所有权为 rust、没有活跃旧工作流、从未投递/尝试/领取、无 Inbox、
无重复操作键、且业务对象与参数准确匹配的批次事件和符合条件的 SAU 投稿。
SAU 还要求无开始、结束、上传、回执证据，无历史 retry_of_job_id，
无同目标其他未核实或成功投稿记录。保持原事件 ID/到期时间，
事务内建立原生 SAU Inbox，不发送外部请求。
已尝试、已投递未确认、参数错误、未知 handler、旧所有权等记录不自动重放，
仍为审计阻塞项；不得把它们标记完成以绕过门槛。

原始 9 个处理器：
- bilibili_publisher.process_job / bilibili_publisher.auto_dub_p2：
  新业务由 Rust 原生执行；历史未决投稿或追加必须独立核实/迁移。
- social_publisher.process_job：原生 SAU 消费；仅明确安全的历史队列可接管。
- subtitle_service.push_publish_workflow_event / subtitle_service.cleanup_task：
  原生批次事件；支持严格历史接管。
- subtitle_service.push_render_workflow_event / subtitle_service.push_dubbing_workflow_event：
  原生工作流结算与渲染回执；历史 Hatchet 通知不能假造为 Rust 回执。
- subtitle_service.after_render_publish / subtitle_service.cancel_workflow_run：
  新 Rust 流程由原生阶段和取消状态机管理；旧手动自动投稿/Hatchet 取消
  仍需历史所有权与外部状态核实，不能直接重放。

## 验证

VIDEOROLL_V2_TEST_OUTBOX_DISPATCHER_ONLY=true bash rust/scripts/smoke-isolated.sh
VIDEOROLL_V2_TEST_PUBLISH_BATCH_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh
VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true bash rust/scripts/smoke-isolated.sh

测试只使用临时 PostgreSQL，覆盖原始 handler 审计、切换保护、锁冲突、
批次及 SAU 接管、旧消费者隔离、幂等和未决事件保留。
生产库未执行接管，旧服务未停止。真实 SAU 采用前轮隔离模拟验收，
本轮未重新执行真实上传；历史任务所有权迁移与最终部署切换尚未完成。
