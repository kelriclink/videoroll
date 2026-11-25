# 主服务原生混合投稿批次（2026-11-25）

基线 `e7c7df0`。新增原 `/api/v1/tasks/{task_id}/actions/publish_all` 中包含 Bilibili 的显式 2–4 平台路径，要求 `VIDEOROLL_V2_NATIVE_OUTBOX_ONLY=true`、`VIDEOROLL_V2_ENABLE_NATIVE_MIXED_PUBLISH=true`、`VIDEOROLL_V2_SAU_ENABLE_NATIVE_PUBLISH=true`，并满足原生 B 站、SAU 平台启用及共享媒体目录等原有条件。部署方仍必须确保两个原生消费者配置正确且旧 Python 消费器已停用；环境变量并不证明进程已运行。仅接收没有既有投稿记录、没有活动工作流且有授权和 final 素材的 Rust 任务。SAU-only 路径保留原行为；不自动选择目标，不开启 force_retry、skip_review、fresh_batch。

复用 Bilibili prepare/enqueue_prepared，和原 SAU 元数据校验及 native Outbox enqueue。任务行锁下同一 PostgreSQL 事务插入原 `publish_batches`、全部 `publish_jobs`、B 站 workflow/stage 与原 recovery fence、SAU native_pending outbox/inbox、active_publish_batch_id；任意校验失败全部业务入队记录回滚。审核等前置检查在事务外执行（其独立审核结果不属于上述回滚保证）。原批次 request_json 保存 runtime marker、规范 JSON 请求 SHA-256 与平台列表，逐目标冻结参数保存在原 publish_jobs/workflow 中。精确重复请求读取原批次回执（包括媒体已清理场景）；不同请求拒绝重新入队。旧 SAU facade、独立 B 站与 SAU intake 增加混合批次所有权保护，防止额外目标或回执重绑。批次完成由同一原 reconciliation 聚合，SAU 终态也可生成原生批次通知与符合条件的清理事件。没有新增物理表、迁移或路由。

任务详情 publish_jobs 对混合批次返回原真实稿件 UUID、批次 UUID、账号和状态；SAU 无确认 ID/URL 且 receipt_verified=false 的 submitted 以 unknown 显示。手工 final 删除增加混合批次待处理及不确定原稿件保护。既有自动 GC 检查仍保留。

## 验收

`VIDEOROLL_V2_TEST_BILI_DIRECT_ONLY=true VIDEOROLL_V2_TEST_MIXED_PUBLISH=true` 第三轮退出 0：原 29 个 B 站 PASS，加两个混合专项。测试用临时 PostgreSQL、真实 MP4、Rust API、Rust B 站 Worker + 本地模拟 UPOS/作者中心，以及 Rust SAU 消费器 + 无网络 CLI。三目标中第三账号无效导致前两个目标记录全部回滚；三个并发相同请求只有一个 batch/同一组 job；改变请求与强制重试被拒绝；B 站先完成、SAU 先完成均保持批次 active 直到其他目标终态；CLI 成功但无真实回执、启动后失败都不能被当作已发布，最终 partial_failed，未生成清理事件；拒绝独立 SAU 加入新目标，原历史列表与手工资产删除保护验证通过。首轮测试地址末尾斜杠断言错误，次轮隔离 schema 未定义原 publish_state 枚举；修正夹具并复用生产字段枚举后通过，不修改生产库。

CONTRACT_217_ONLY 退出 0：204 正向、12 拒绝、1 主动阻断、0 异常；SAU_NATIVE_WORKER_ONLY 退出 0，包括中断恢复、未确认回执与三平台专项。cargo check --locked、fmt --check、Python py_compile、bash -n、git diff --check 通过。日志 `/4t/videoroll-rust-mixed-publish-target/MIXED-r3.log`、`CONTRACT_217.log`、`SAU_NATIVE_WORKER.log`；隔离 PG 35486。

## 未签收与下一步

原生 SAU CLI 退出 0 仍只提供 submitted/unverified，混合批次不能自动获得平台 ID/URL。本轮未实现混合原稿件的独立平台回执核验/人工确认入口及确认后重新结算；未验证真实全目标成功后清理，不能宣称生产端到端等价。既有原生 B 站批次专项覆盖预置已确认 SAU 回执后的成功聚合，但不是本轮真实 SAU 回执采集。纯 SAU-only 仍旧 facade；旧历史任务/单平台批次不会自动接管为 mixed；不同请求需人工核验而非强制重放。混合入队冻结账号 ID，仍沿用原 SAU 凭据执行时读取机制与默认 B 站凭据规则；账号轮换和审核设置/素材变动窗口、跨入口并发压力及生产部署开关需要继续审计。未运行完整 smoke、前端构建、真实登录/投稿、生产配置克隆或部署。未修改生产数据库或推送。原 217 路由全业务等价签收仍不能由 HTTP 覆盖率替代。
