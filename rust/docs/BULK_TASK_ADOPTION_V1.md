# 历史任务批量接管（2026-11-25）

基线 14e71a2。新增本地管理 CLI `task-ownership-bulk`，无 API 入口、无新业务表及迁移、无外部请求。

## 操作约定

- 默认只读、REPEATABLE READ，UUID 游标分页，默认 200 条、上限 500 条。输出全库 Python 所有权剩余数、该页分类/阻断原因/恢复候选、next_after 及 SHA-256 plan_token。
- 仅安全终态 published/failed/canceled 批量接管；published 必须存在原 published 投稿及外部回执。复用原逐任务阻断谓词，额外阻断 tasks 残留锁、待下载 inbox、已有接管回执冲突。其他状态留待逐任务审核与恢复。没有文件校验的批量接管不调度任何恢复任务。
- 执行要求 standalone、native outbox only、完整 SQLx 校验和记录、显式 apply/quiesced 和精确审核计划 token。token 是页内审核决策校验码，不是授权密钥或整个数据库快照；实际执行在原业务表 NOWAIT 锁内重新读取并比较。每页独立原子事务，旧计划失效、持锁冲突或任一记录失败均整页回滚。每条安全任务保留原状态、UUID、业务记录，仅更新 runtime_owner/runtime_origin/updated_at，并使用原 operation_inbox 的唯一 operation_key 留下持久化接管回执和计划 token。与单任务接管兼容；不生成工作流、不清除租约、不退役历史运行、不伪造平台回执。
- 操作员必须确认停止全部旧/新生产者及消费者；工具不能证明外部服务已停止。迁移 ledger 校验位于页执行事务前，禁止与迁移/部署同时执行。
- 逐页先检查计划，按同一 limit/after 应用后读取 next_after；最后从起点重新审计全部残留，不以游标已结束证明全库无阻断。中断/已提交但未收到输出时重新生成计划，已接管任务不会重复执行。最终仍需要原 standalone-preflight、独立 outbox 审核及人工回执核对，不得强制标成功。

示例（仅隔离演练，生产实际执行另需明确批准）：

```bash
videoroll-v2 task-ownership-bulk --limit 200
videoroll-v2 task-ownership-bulk --limit 200 --apply --quiesced --plan-token <REVIEWED_TOKEN>
videoroll-v2 task-ownership-bulk --limit 200 --after <NEXT_AFTER>
```

## 验证

- cargo check --locked --offline --all-targets 通过。
- 真实生产 schema-only DDL 与脱敏配置隔离演练：39 迁移、零 vr2 物理表、原接管、渲染终态、outbox、API 回归均通过。
- 新增批量测试：5083 合成历史任务（非真实记录），5075 安全终态接管、8 条需人工处理/恢复候选保留，12 页；首批故障测试之后的其余分页计划及执行循环耗时 47.67 秒。该样本并未模拟生产全部 10415+ outbox 记录、投稿分布或存储状态，不可作为生产耗时及可接管比例预测。
- 验证 stale token、无 quiesced/无 token/非 standalone 拒绝、页上限、行写入锁争用、注入第二条记录失败导致整页回滚、两个并发同一 token 仅一次提交、旧计划重放拒绝、单任务回执复用。最终接管凭证数量正确，无新增工作流，未知投稿、待下载 inbox、历史租约及 launch_failed 证据未改变。
- 日志共享 target/bulk-adoption-check.log、bulk-adoption-schema.log、bulk-adoption-lib.log。未执行生产写入、真实投稿、部署或推送；本轮重新执行真实 DDL 专项与库测试，不是重新签收 217 全量接口业务等价。

## 后续

生产全量备份恢复演练、collation 修复、真实历史异常凭证及完整配置联调仍待完成。仍不得跳过人工核验或强制解除正式切换门槛。
