# 自动多平台投稿接入原生批次（2026-11-25）

基线 `b4fd9f1`。在既有 native-only、native mixed 与 native SAU 开关开启时，原自动 Publish stage 和 V1 auto_publish action 支持含 SAU 的平台组合；Bilibili-only 保留既有路径。读取原自动配置及工作流覆盖，拒绝无效平台值；无目标或关闭继续 skip。选择最新创建的 active/valid SAU 账号，优先已保存元数据，否则使用原生 source draft；读取来源 uploader、按现有平台规则归一化、使用现有审核/封面/分区配置。自动双 P 使用原成功配音证明，支持旧 V1 action 显式选择前代成功配音 job，Bilibili 总标题采用原 Python 【熟肉&中配】及 P1/P2 标题规则；SAU 仅用 P1。未实现全部 Python 平台专属历史元数据文件/AI 标签与话题生成语义。

复用原 `publish_batches`、`publish_jobs`、原生 SAU Outbox 和 Bilibili admission。Task 行锁下检查当前父工作流及运行中 Publish job 的确切有效 lease，排除且仅排除该父任务的活动工作流冲突；批次 request_json 保存父 workflow/job/generation，全部目标持久化及原父 job 转 awaiting、`native-batch:<id>` ticket 在同一事务提交。混合目标复用独立 Bilibili 执行子工作流；纯 SAU 无 Bilibili 子工作流。批次 reconciliation 从原目标回执更新原父阶段：active 保持等待，失败/未确认标记 unknown，全部确认才 succeeded；不再重复创建父 cleanup stage，沿用批次的原生 24 小时保留事件。拒绝通用外部回调、通用 unknown 重试及已入队父工作流暂停/取消；普通超时扫描不误判 native-batch awaiting，真实子任务仍有各自租约恢复。没有新表或迁移。

隔离测试 `VIDEOROLL_V2_TEST_BILI_DIRECT_ONLY=true VIDEOROLL_V2_TEST_MIXED_PUBLISH=true` 最终退出 0，35 个 PASS 标记：原 Bilibili/混合/纯 SAU 全部通过；新增原 V1 auto action 纯 SAU、原流水线 Publish stage 混合批次、最新有效账号选择、持久化父票据、消费者退出后人工回执恢复、原批次清理事件及前代显式 P2 双分 P 原标题。第二轮跨代 P2 fixture 遗漏原生 current_stage 导致 500，第三轮旧 mock 的固定手动 P2 标题断言拒绝实际请求；修正 fixture 并按原 Python 标题算法新增独立精确断言后通过。`CONTRACT_217_ONLY` 退出 0：204 正向、12 拒绝、1 隔离主动阻断、0 异常。cargo check、实际编译、fmt、py_compile、git diff --check 通过。日志 `/4t/videoroll-rust-auto-batch-target/AUTO_BATCH-r4.log`、`CONTRACT_217.log`；隔离 PG 35489。

限制：仍需上述显式部署开关。Bilibili-only 的自动路径、所有历史 Python draft/平台专属 meta 文件与 AI 标签/活动、无语音远程链路仍需独立等价验收。自动 stage 在入队前遇到错误仍沿用原 publish unknown 隔离策略，不自动重试；已持久化的 batch 只从子回执推进，未验证真实断电/长时间故障、所有控制入口竞态、双 P 自动配音从下载开始的完整链路或所有三个 SAU 平台的自动组合。SAU 成功退出不是自动平台回执，人工确认仍必需。当前没有真实平台、生产配置、完整 smoke 或前端验收；没有生产数据库写入、部署、推送。
