# 通用任务停止与远程渲染租约收尾（2026-11-25）

基线 `3e81b12`。原任务 stop/resume 与 V2 workflow pause/cancel 以前仅改变父状态，可能留下仍能续租的 Worker。现在暂停与终止在 Task -> Workflow -> Inbox -> Execution 的锁顺序下同步改变原生 render_executions 的状态，限定 exact task/workflow/job/ticket 关联，历史 Python 执行不受影响。

- V1 stop 是可恢复暂停，保留 stopped_status、同一 workflow generation、原 render ticket/attempt/已上传证据。执行进入 paused，不再续租、完成或提交新上传；Worker cancel 轮询报告停止，原 fence 的 fail 仅确认停止，保留 paused 状态。即使立即 resume，也要等原租约释放；render-tick 在父状态可运行且原租约已释放时恢复 queued，非上传执行清除旧 Worker/fence，已上传执行保留证据并走既有完整性校验与回执恢复。不重置五次预算，达到预算的未上传任务恢复时走原失败结算。没有新任务、表或迁移。
- V2 workflow cancel 是终止，原 awaiting render 与父工作流一起进入 canceled，清除可恢复 stopped_status；旧租约保留直到确认/到期，不自动重排。已登记输出与已完成回执保留作证据，不删除或继续发布。已有外部发布不确定分支不变。
- Worker 活跃槽位计数包含仍持有有效租约的 paused/canceled 执行；claim 额外核对父任务归属与准确阶段/票据关联，构造 spec 后再次检查 fence。noop 最终资产登记与完成回执改为同一事务、同一父锁顺序，避免被暂停/取消的旧 Worker 登记最终资产。完成时释放租约。
- 通用资产删除、存储回收安全检查、终态存储清理均保护有效渲染租约；前端展示 paused 状态。已登记上传资产仍有 FK 和存储引用保护，取消后证据清理策略未放宽，不宣称已自动清除所有残留。没有实际远程 GPU、长传输并发/网络分区或生产环境验证；其他非渲染阶段停止语义与历史无归属票据仍需专项核查。旧 Worker 不确认停止时依赖原租约到期，不能证明远端进程已实际退出。

验收：独立 target `/4t/videoroll-rust-render-task-stop-target`，临时 PostgreSQL 35476。`VIDEOROLL_V2_TEST_RENDER_EVENTS_ONLY=true` 与 `VIDEOROLL_V2_TEST_RENDER_ADMIN_ONLY=true` 全部通过，含真实 MP4 第五次上传后暂停/立即恢复/确认停止/唯一回执、V1 stop/resume、迟到 heartbeat/complete、错误 fence、原预算、终止后不可恢复、槽位释放、资产删除保护、历史 P2 取消及原渲染回执回归。Rust 编译/格式、Python 语法与 git diff 检查通过。未运行全 217 接口业务差分、前端完整构建或原生 P2 投稿完整回归；未修改生产数据库。
