# 原生产结构与历史接管演练（2026-11-25）

基线 338df33。生产 PostgreSQL 18.4 / pgvector 0.8.4，只读审计及 schema-only 快照，35 张原表，无 SQLx 迁移记录。快照位于开发机私有目录 /4t/vr-cutover-schema.77ysfe，原 schema.sql SHA256 c8bb7fcafd7f5f062c6e4b9199b0366e8e716c6cb4233aabf11dfa056e668fe7。没有导出生产业务数据、真实账号、Cookie、密钥、外部服务地址或用户内容。controls.json 包含 10 个设置对象的 66 个白名单字段。

## 修复
- Rust 创建 local/YouTube task 时显式写入 retry_count=0，原生产字段 NOT NULL 无服务端默认值。
- 原 V1 和 V2 YouTube source 插入显式初始化四个扫描计数，更新路径仍保留原计数。
- 批次 outbox 回执显式写入原 NOT NULL request_json，并记录原 outbox_event_id。
- 原 Python 渲染执行终态为 succeeded；接管审核及独立启动门槛只在 runtime_engine=python 且 finished_at 非空时认定此成功状态为终态。历史状态和租约时间戳不改写，缺完成时间的执行仍拒绝，原 render_job 租约等独立阻断不取消。

## 验证
使用独立 PG 容器及命名卷，恢复可信 schema-only SQL（pg_dump 的 public schema 筛选不包含 vector 扩展声明，隔离恢复显式创建），迁移之前插入合成任务、原回执、历史工作流、批次事件、真实微型视频和 SRT。没有复制真实任务行。严格验证真实 DDL 的 39 次迁移、零 vr2 物理表、原配置和 ID 保留、接管幂等、媒体校验、未知回执和不完整渲染保护、显式确认已停止历史运行、批次接管与持久化回执、V1/V2 API 新建任务/来源和生产控制字段读回。合成未知投稿只有在模拟操作员提供回执后才通过初始切换门槛。第一次新增渲染夹具重复 fence token 触发真实唯一约束，改用独立 token 后最终专项通过，未改变约束。

- 最新真实 DDL 专项：cutover-schema-final-r2.log，退出 0（随后单独执行库测试）。
- 批次回归：cutover-batch-regression.log 通过，真实隔离资源 GC、重复防护及历史事件接管。
- 完整 smoke：cutover-full-regression.log 退出 0，153 PASS / 54 PASS DIFF 日志标记；217 探测 125 正向、91 拒绝路径、1 主动阻断、0 异常；完整签收 0。此轮完整 smoke 使用加入渲染终态修复之前的二进制；最终修复另经真实 DDL 专项验证。
- 日志均位于共享 rust/target。无生产写入或真实投稿。隔离配置只验证部分控制字段及原值保留，未测试真实生产 API 密钥、所有配置、全部历史数据或真实平台。真实 DDL 不是完整生产数据备份恢复。

## 生产只读审计的剩余阻断
- 5083 个任务；60 条不确定投稿（24 B站 unknown、10 抖音 unknown、26 抖音 submitted 无外部回执）；完整任务投稿阻断条件命中 62 条投稿记录、57 个任务。
- 355 条非已知终态 Hatchet 工作流涉及 8 个任务：353 launch_failed（6 个任务）、1 published、1 running；需逐项确认真实终态或外部停机证据，不能直接标成功。
- 原审计误判 936 条 succeeded 渲染执行；均有 finished_at，按修正后的规则执行层阻断为 0。另有 4 条渲染 job 和 1 条字幕 job 保留过期租约；不可自动推断安全释放。
- 10415 条 outbox 有 completed inbox 回执；3 条未尝试，另 5 条需核对（3 条已分发但 inbox failed，2 条已分发缺 inbox）。分类不表示实际外部平台成功。
- 生产数据库 collation 记录 2.41、当前 2.36：仍需备份及受控索引重建/版本修复演练；没有修改生产数据库。
- 未进行生产全量数据恢复、真实历史接管、全部接口正向等价签收或正式部署；无推送。不得通过批量覆写未知投稿或伪造回执绕过准入。
